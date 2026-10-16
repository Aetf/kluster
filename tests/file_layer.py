"""The credential file layer, closed for the whole test process, and the blocks that open it.

The acquisition chain's second layer (`kluster.lib.acquisition`) is a file in
the checkout's `.credentials/`, and on an operator's workstation the checkout
a test process runs from holds the operator's own: the account roots, the kit,
the operator passphrase, the client bundle. `close` shuts that layer for the
process, before any test module is collected, by replacing the one function
that forms a checkout's `.credentials/`, `kluster.lib.workstation.slots`, and
by removing the variables that carry a path into it. `tests/conftest.py` calls
it from `pytest_configure`, and undoes it from `pytest_unconfigure`
(framework/testing.md §1.1).

The replacement is keyed on location. Asked for the checkout the process runs
from, it answers `CLOSED`, a directory below a device file: every slot in it is
absent, and the operating system creates nothing there, for root as for
anyone. Asked for any other checkout -- one a case built under its `tmp_path`
-- it answers that checkout's own, because that layer is the case's.

A case that needs the running checkout's layer opens a block of its own over
the closed one: `opened`, on a directory the case owns, or `live` for the
drills, which read and rotate the operator's kit. A named module rather than
part of `conftest`, because test modules import from it and `conftest` is not
a name an import can aim at -- the reason `memory_keyring` is one.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import root_credentials

from kluster.lib import workstation

if TYPE_CHECKING:
    from collections.abc import Generator

#: The running checkout's `.credentials/` while the layer is closed: below a
#: device file, so every slot is absent and nothing can be created, and a
#: refusal names a path that says why.
CLOSED = Path(os.devnull) / workstation.DIRECTORY

#: The production function, captured before anything replaces it: what every
#: checkout other than the running one is answered with, and what `live`
#: opens the running one's real layer through.
_SLOTS = workstation.slots


@dataclass
class _Layer:
    """What `close` found, and the blocks opened over it since, innermost last."""

    #: The checkout the process runs from, resolved; `None` while not closed.
    running: Path | None = None
    #: The path variables as the process received them, for `live` to hand back.
    received: dict[str, str] = field(default_factory=dict)
    opened: list[Path] = field(default_factory=list)


_LAYER = _Layer()


def _slots(checkout: Path) -> Path:
    """`workstation.slots` while the layer is closed: the running checkout's is the innermost block's, else `CLOSED`."""
    if checkout.resolve() != _LAYER.running:
        return _SLOTS(checkout)
    return _LAYER.opened[-1] if _LAYER.opened else CLOSED


def close(patch: pytest.MonkeyPatch) -> None:
    """Close the running checkout's file layer through `patch`, whose `undo` opens it again.

    The running checkout is resolved here, once, the way `directory()`
    resolves it. The variables removed are `root_credentials.UNMASKED_PATHS`:
    left unmasked as values, because a path discloses nothing, they are still
    the way a process a case starts would follow its way into the layer.
    """
    _LAYER.running = workstation.repo_root().resolve()
    _LAYER.received = {name: os.environ[name] for name in root_credentials.UNMASKED_PATHS if name in os.environ}
    _LAYER.opened.clear()
    patch.setattr(workstation, 'slots', _slots)
    for name in root_credentials.UNMASKED_PATHS:
        patch.delenv(name, raising=False)


@contextmanager
def opened(directory: Path) -> Generator[Path]:
    """`directory` as the running checkout's `.credentials/` for the block, for every reader.

    Not created by opening it, as `directory()` creates nothing. Blocks end
    innermost first, and the end of one puts back what the enclosing block
    opened -- the closed layer outside every block -- without resolving
    anything, which is the discipline `memory_keyring.installed` keeps.
    """
    _LAYER.opened.append(directory)
    try:
        yield directory
    finally:
        ended = _LAYER.opened.pop()
        assert ended is directory, 'a file layer block ended out of order'


@contextmanager
def live() -> Generator[Path]:
    """The running checkout's own `.credentials/`, and the path variables as received, for the block.

    For `tests/live/` alone: a drill opens and rotates the operator's kit,
    through `$KLUSTER_KDBX` or the kit's slot, and is collected only on the
    operator's say-so (`RUN_LIVE_DRILLS`).
    """
    assert _LAYER.running is not None, 'the file layer was never closed, so there is nothing to reopen'
    with pytest.MonkeyPatch.context() as patch, opened(_SLOTS(_LAYER.running)) as real:
        for name, value in _LAYER.received.items():
            patch.setenv(name, value)
        yield real
