"""A tool `mise.toml` pins, refused by name when a case needs it and it is missing.

The gate runs the suite under `mise x`, where every pinned tool is on `PATH`,
so a missing one says the run is not the gate's rather than that the cases
needing it may be left out. A skip there would keep a run outside mise green
with those cases never run; `require` fails the case instead, naming the
tool and where it is pinned.

It is a fixture's body (testing.md §2): a module wraps it in a fixture of its
own and marks the cases that need the tool, so a run without the tool still
runs that module's other cases. `located` is the same refusal for a case that
runs the tool by the path it resolves to, and hands that path back.

Where `pulumi` is left out rather than refused is testing.md §8's to say; a
case it lets skip does not come here.
"""

from __future__ import annotations

import shutil

import pytest

from kluster.scripts.credentials import age

#: The two binaries the `age` pin installs: `age-keygen` draws an identity,
#: and `age` encrypts to a recipient and decrypts with an identity.
AGE = (age.BINARY, age.KEYGEN)


def located(binary: str) -> str:
    """The path `binary` resolves to on `PATH`; where it resolves to none, fails the calling case naming it."""
    found = shutil.which(binary)
    if found is None:
        pytest.fail(f'{binary} is not on PATH: mise.toml pins it, so run the suite under `mise x`')
    return found


def require(*binaries: str) -> None:
    """Fails the calling case, naming the first of `binaries` that is not on `PATH`."""
    for binary in binaries:
        _ = located(binary)
