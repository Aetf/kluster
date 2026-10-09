"""What every suite in this process shares: an empty environment, and a cheap KDF.

The first is `root_credentials.strip`, called below rather than offered as a
fixture, so that no suite can be the one that forgot to ask. What it takes is
that module. Of the two channels it leaves open, the desktop secret store is
closed here instead, for the whole session (`secret_store_closed`): the store
is read and written at test time, never at import, so a fixture reaches it. The file
layer stays each suite's own to redirect.

The kit several suites share is `memory_kit.MemoryKit`, a kit that is not a
file; it lives in its own module because test modules import the class
directly, and `conftest` is not a name an import can aim at.

The other thing shared here is the cost of a key derivation. Several suites do
want a real KeePass file rather than the in-memory stand-in -- the row shape is
half of what they check -- and a KDBX4 file is guarded by Argon2 at settings
chosen to be slow. `cheap_kdbx_kdf` moves that cost to the algorithm's floor
for the whole session. The same holds for RSA: the credential suites mint OCI
keys hundreds of times over, and `keys_from_the_pool` hands each case keys
generated once per process rather than once per call.

Next is a watch on a process-global the suites share without meaning to:
`pickler_left_as_found` names the case that changes the pickler, and two hooks
name a fixture wider than a case that does, so that the residue fails what made
it rather than whichever case happens to run after it.

The last is a workaround for the Pulumi SDK, kept apart so that it can be
deleted whole: `mocked_runs_release_their_tasks` drops from the SDK's set of
tracked outputs every task whose event loop has closed.
"""

# `pykeepass` ships no type information; the store module carries the same
# waiver for the same reason.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import os
import pickle
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import keyring.backends.fail
import pykeepass.pykeepass as pykeepass_module
import pytest
import root_credentials
from memory_keyring import installed
from memory_kit import MemoryKit
from pulumi.runtime.settings import SETTINGS
from pykeepass import PyKeePass

from kluster.scripts.credentials import oci_iam
from kluster.scripts.credentials.kdbx import KdbxStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from contextlib import AbstractContextManager

# At import rather than in a fixture, even a session-scoped autouse one: the
# root `conftest` is imported before any test module, whereas the first
# fixture runs after all of them have been imported, so a suite that read a
# variable while being collected would still have seen the operator's value.
# Nothing is put back afterwards, because the process this strips is the test
# run itself.
#
# This file is what makes it reach a suite, so it reaches the suites under
# `tests/` and no others. That is the whole of the suite today and there is no
# conftest above this one; a test module added outside this tree would be
# outside this too.
root_credentials.strip(os.environ)

#: Argon2 at its cheapest, in the names KDBX gives the parameters: one pass
#: (`I`), one lane (`P`), and the least memory the algorithm accepts for one
#: lane -- 8 KiB, written as the byte count the format stores (`M`). The
#: library's own template asks for 14 passes over 64 MiB in 2 lanes, which is
#: a fifth of a second per derivation.
FLOOR = {'I': 1, 'M': 8192, 'P': 1}


@pytest.fixture
def memory_kit() -> KdbxStore:
    return MemoryKit()


@pytest.fixture(autouse=True, scope='session')
def secret_store_closed() -> Iterator[None]:
    """A desktop secret store that refuses every call, for the whole session.

    `keyring` resolves the operator's own store unless told otherwise, and a
    `credentials` command writes it (`generate` and `recover` of a row the
    acquisition chain reads, `root remember`, `kit password remember`), so a
    case that reaches one of those would replace a live value with a
    placeholder -- and a case that reads the chain would take whatever the
    operator keeps there. Refusing every call is what a machine with no store
    does, which every caller already handles. A case that needs a store
    installs the in-memory one from `memory_keyring` for its own length, over
    this one.

    Session-scoped and autouse, so it is in place before any other fixture of
    any scope is set up and taken away after the last is torn down: a module's
    fixture between two cases meets it as the cases do.
    """
    with installed(keyring.backends.fail.Keyring()):
        yield


def _parameters(database: PyKeePass) -> Any:
    """The KDF parameters in a loaded database's header, as pykeepass parsed them."""
    kdbx = database.kdbx
    assert kdbx is not None, 'the database was constructed without being loaded'
    return kdbx.header.value.dynamic_header.kdf_parameters.data.dict


def _cost(database: PyKeePass) -> dict[str, int]:
    """The Argon2 cost this database's header declares."""
    parameters = _parameters(database)
    return {name: int(parameters[name].value) for name in FLOOR}


@pytest.fixture(scope='session', autouse=True)
def cheap_kdbx_kdf(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Every database the suite creates derives its key at floor cost.

    `pykeepass.create_database` copies a blank database shipped with the
    library, so that template's KDF settings become the settings of every kit
    the suite writes, and are paid again on every open and every save -- a
    write-heavy test derives a dozen keys, at a fifth of a second each.
    Rewriting the template once per session at the floor above makes each of
    those derivations take well under a millisecond.

    Self-consistent by construction: KDBX4 records the cost parameters in the
    file header, so a database created from this template is opened and saved
    with the parameters it was written with, by the same unpatched pykeepass
    every other caller uses. Nothing here weakens a database that exists
    outside the suite -- the tests create their kits in their own temporary
    directories and never open an operator's, and `KdbxStore.create` in
    production still reaches the library's own template.
    """
    template = PyKeePass(pykeepass_module.BLANK_DATABASE_LOCATION, pykeepass_module.BLANK_DATABASE_PASSWORD)
    parameters = _parameters(template)
    for name, value in FLOOR.items():
        parameters[name].value = value
    blank = tmp_path_factory.mktemp('kdbx-template') / 'blank.kdbx'
    template.save(str(blank))
    # The lines above reach into a parsed header, so the round trip is checked
    # rather than assumed: a pykeepass that rebuilt those bytes from anywhere
    # else would hand the suite production costs back without failing anything.
    written = _cost(PyKeePass(str(blank), pykeepass_module.BLANK_DATABASE_PASSWORD))
    assert written == FLOOR, f'the template kept {written} rather than {FLOOR}'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pykeepass_module, 'BLANK_DATABASE_LOCATION', str(blank))
        yield


@pytest.fixture(scope='session')
def key_pool() -> list[oci_iam.KeyPair]:
    """The OCI key pairs this process has generated for its cases, in the order they were first drawn."""
    return []


def drawing(pool: list[oci_iam.KeyPair], generate: Callable[[], oci_iam.KeyPair]) -> Callable[[], oci_iam.KeyPair]:
    """A `generate_key` for one case: `pool`'s keys from the first, each once, then fresh ones added to it."""
    drawn = 0

    def draw() -> oci_iam.KeyPair:
        nonlocal drawn
        if drawn == len(pool):
            pool.append(generate())
        drawn += 1
        return pool[drawn - 1]

    return draw


@pytest.fixture
def generated_keys() -> None:
    """Asked for by a case about `oci_iam.generate_key` itself, which then mints for real."""


@pytest.fixture(autouse=True)
def keys_from_the_pool(key_pool: list[oci_iam.KeyPair], request: pytest.FixtureRequest) -> Iterator[None]:
    """`oci_iam.generate_key` draws from the process's pool of keys for the length of the case.

    A 2048-bit key takes tens of milliseconds to generate, and what the cases
    that mint one are about is the order of the calls around a key rather than
    the arithmetic inside one. A case draws the pool's keys in order, each
    once, so every call within it is a key of its own -- the cases tell keys
    apart by fingerprint -- and a case that draws past the end generates the
    next key into the pool, so it stays correct and is only as slow as it was
    without one. The keys are `generate_key`'s own, at the production size,
    so nothing downstream sees a different key shape; they are shared across
    cases and never across processes, and the tenancies, kits and stores they
    land in are each case's own. A case asking for `generated_keys` is left
    the real function.
    """
    if 'generated_keys' in request.fixturenames:
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(oci_iam, 'generate_key', drawing(key_pool, oci_iam.generate_key))
        yield


#: The pickler `dill` derives from and the one Pulumi's provider serialization
#: patches. Private to `pickle`, and typed as nothing but a class because what
#: is watched below is its whole attribute dict rather than any named method.
PICKLER: type[Any] = pickle._Pickler  # pyright: ignore[reportPrivateUsage]


class _Absent:
    """What an attribute the pickler does not carry compares as.

    Its own value rather than `None`, so that an attribute added holding `None`
    and one removed are each a change, and each reads as one in the message.
    """

    def __repr__(self) -> str:
        return 'absent'


ABSENT = _Absent()


def _pickler() -> dict[str, Any]:
    """The pickler's attributes, as they are now."""
    return dict(vars(PICKLER))


def _described(changed: list[str], before: dict[str, Any], after: dict[str, Any]) -> str:
    """A failure message's tail: which attributes changed, from what to what."""
    return f'left the pickler changed at {changed}: ' + '; '.join(
        f'{name}: was {before.get(name, ABSENT)!r}, now {after.get(name, ABSENT)!r}' for name in changed
    )


def _differ(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """The attributes `after` holds something else under than `before` does, by identity."""
    return {name for name in before.keys() | after.keys() if before.get(name, ABSENT) is not after.get(name, ABSENT)}


@pytest.fixture(autouse=True)
def pickler_left_as_found() -> Iterator[None]:
    """A case that leaves the pickler changed fails by its own name.

    Pulumi's `serialize_provider` replaces methods on `pickle._Pickler` and
    puts none of them back (its `finally` restores the `pickle.Pickler` name
    alone; `kluster.providers.serialization` has the rest). Production is
    covered by the shim `kluster.providers` installs on the module attribute;
    a case that reaches the bare function -- a name bound by a from-import
    before that package was imported, say -- leaves the wrappers behind for
    every case after it in the process. Residue like that fails some later
    case with text naming this one's leftovers, and only in the collection
    orders where that case comes later. Checked after each case, it fails the
    case that leaked, whatever the order, naming what changed. A fixture wider
    than a case is held across its own life by the two hooks below.

    The whole attribute dict rather than the names the shim restores: what is
    held is that the class is as the case found it, so a method Pulumi starts
    patching in a later release is caught here without anyone listing it. The
    comparison is by identity, which is what a leaked wrapper fails and what a
    case that restored the original satisfies.
    """
    before = _pickler()
    yield
    after = _pickler()
    changed = sorted(_differ(before, after))
    assert not changed, f'the case {_described(changed, before, after)}'


@dataclass
class _Life:
    """The pickler at the points of one wider fixture's life that tell its own changes from others'."""

    #: Before its set-up.
    found: dict[str, Any]
    #: After its set-up.
    left: dict[str, Any]
    #: As its teardown finds it.
    torn: dict[str, Any] | None = None


_LIVES: dict[pytest.FixtureDef[Any], _Life] = {}


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef[Any]) -> Generator[None, object, object]:
    """Take the pickler around the set-up of a fixture wider than a case, and again as its teardown begins.

    Such a fixture is set up before the first case that asks for it, so a
    serialization it runs then is outside every case's own watch. The
    finalizer added here runs before the fixture's own teardown, which pytest
    registered during the set-up, since finalizers run last-added first.
    """
    if fixturedef.scope == 'function':
        return (yield)
    found = _pickler()
    result = yield
    life = _Life(found=found, left=_pickler())
    _LIVES[fixturedef] = life

    def teardown_begins() -> None:
        life.torn = _pickler()

    fixturedef.addfinalizer(teardown_begins)
    return result


def pytest_fixture_post_finalizer(fixturedef: pytest.FixtureDef[Any]) -> None:
    """A fixture wider than a case that leaves the pickler changed fails by its own name, at its teardown.

    What is its own is what its set-up changed and still holds the value the
    set-up left, and what its teardown changed to anything but what the
    set-up found. So a fixture that patches for its life and restores at its
    teardown passes, and a change made inside its life by a case, a narrower
    fixture, or a wider fixture first set up meanwhile is that one's to answer
    for, not this one's.
    """
    life = _LIVES.pop(fixturedef, None)
    if life is None:
        return
    now = _pickler()
    torn = life.torn if life.torn is not None else now
    kept = {name for name in _differ(life.found, life.left) if now.get(name, ABSENT) is life.left.get(name, ABSENT)}
    torn_down = {name for name in _differ(torn, now) if now.get(name, ABSENT) is not life.found.get(name, ABSENT)}
    changed = sorted(kept | torn_down)
    assert not changed, (
        f'the {fixturedef.scope}-scoped fixture {fixturedef.argname!r} {_described(changed, life.found, now)}'
    )


def release_closed_loops_tasks(tracked: set[asyncio.Task[Any]], lock: AbstractContextManager[object]) -> None:
    """Drop from `tracked`, under `lock`, every task whose event loop has closed, and nothing else."""
    with lock:
        tracked.difference_update({task for task in tracked if task.get_loop().is_closed()})


@pytest.fixture(autouse=True)
def mocked_runs_release_their_tasks() -> Iterator[None]:
    """After each case, the SDK's tracked outputs hold no task of a loop that has closed.

    Every `pulumi.Output` adds the task computing it to `SETTINGS.outputs`,
    and takes it out again only when it succeeds: a cancelled or failed one
    is left there for `wait_for_rpcs` to collect when a program exits. A
    mocked run never exits that way -- its loop closes at the end of the case
    and cancels whatever was still pending -- and the property resolves to one
    set for the whole process, so every case's leftovers stay reachable from
    it for the rest of the run, and `asyncio.all_tasks()` walks them all on
    every call. A task of a closed loop can never be awaited again, so
    dropping it is invisible to the SDK; a task of a loop still open, a
    module's shared run included, is left where it is.

    The layer's boundary is this fixture and `release_closed_loops_tasks`;
    nothing else in the suite knows the set exists. It comes out whole --
    both, and the import of `SETTINGS` -- when a locked `pulumi` stops
    leaving them there, which `tests/test_tracked_outputs.py` shows: its child
    run passes with this fixture deleted.
    """
    yield
    # Both are declared through the SDK's own `contextproperty` decorator,
    # which the checker cannot read as an attribute of `Settings`.
    tracked: set[asyncio.Task[Any]] = SETTINGS.outputs  # pyright: ignore[reportAttributeAccessIssue]
    lock: AbstractContextManager[object] = SETTINGS.lock  # pyright: ignore[reportAttributeAccessIssue]
    release_closed_loops_tasks(tracked, lock)
