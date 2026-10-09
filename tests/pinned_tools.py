"""A tool `mise.toml` pins, refused by name when a case needs it and it is missing.

The gate runs the suite under `mise x`, where every pinned tool is on `PATH`,
so a missing one says the run is not the gate's rather than that the cases
needing it may be left out. A skip there would keep a run outside mise green
with those cases never run; `require` fails the case instead, naming the
tool and where it is pinned.

It is a fixture's body (testing.md §2): a module wraps it in a fixture of its
own and marks the cases that need the tool, so a run without the tool still
runs that module's other cases.
"""

from __future__ import annotations

import shutil

import pytest

from kluster.scripts.credentials import age

#: The two binaries the `age` pin installs: `age-keygen` draws an identity,
#: and `age` encrypts to a recipient and decrypts with an identity.
AGE = (age.BINARY, age.KEYGEN)


def require(*binaries: str) -> None:
    """Fails the calling case, naming the first of `binaries` that is not on `PATH`."""
    for binary in binaries:
        if shutil.which(binary) is None:
            pytest.fail(f'{binary} is not on PATH: mise.toml pins it, so run the suite under `mise x`')
