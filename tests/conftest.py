"""Fixtures shared across the credential suites.

The kit they share is `memory_kit.MemoryKit`, a kit that is not a file; it
lives in its own module because test modules import the class directly, and
`conftest` is not a name an import can aim at.

The other thing shared here is the cost of a key derivation. Several suites do
want a real KeePass file rather than the in-memory stand-in -- the row shape is
half of what they check -- and a KDBX4 file is guarded by Argon2 at settings
chosen to be slow. `cheap_kdbx_kdf` moves that cost to the algorithm's floor
for the whole session.
"""

# `pykeepass` ships no type information; the store module carries the same
# waiver for the same reason.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pykeepass.pykeepass as pykeepass_module
import pytest
from memory_kit import MemoryKit
from pykeepass import PyKeePass

from kluster.scripts.credentials.kdbx import KdbxStore

if TYPE_CHECKING:
    from collections.abc import Iterator

#: Argon2 at its cheapest, in the names KDBX gives the parameters: one pass
#: (`I`), one lane (`P`), and the least memory the algorithm accepts for one
#: lane -- 8 KiB, written as the byte count the format stores (`M`). The
#: library's own template asks for 14 passes over 64 MiB in 2 lanes, which is
#: a fifth of a second per derivation.
FLOOR = {'I': 1, 'M': 8192, 'P': 1}


@pytest.fixture
def memory_kit() -> KdbxStore:
    return MemoryKit()


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
