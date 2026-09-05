"""What a test process may see of the operator's credentials: none of them.

`mise.toml` materializes the account-root token into `GITHUB_TOKEN` for every
process it starts, and it does so from a file rather than from the caller, so
it wins over anything set on the command line. A `pytest` run started the way
AGENTS.md requires therefore carries a live account root whether the suite
wants one or not, and the same holds for the passphrase that decrypts every
stack's config and the backend URL that names a client key.

That is only a hazard because Pulumi prints a resource's inputs when an
assertion about it fails, and a provider's inputs include its credential: the
first failing assertion in a suite that declares a provider renders whatever
the environment was holding into the report.

So the masking is a property of the test environment rather than of each
suite that remembers to ask for it. `tests/conftest.py` calls `strip` at
import, before any test module is imported, and every suite in the process
runs with the variables below absent. A suite that needs a value asks for a
fake one by name through `fake_credentials`; a suite that needs one and does
not ask gets whatever the code under test raises for an unset variable, which
names the variable it wanted -- `kluster.stacks.github` is the worked example.

This module is a named module rather than part of `conftest`, because test
modules import from it and `conftest` is not a name an import can aim at.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from kluster.scripts.credentials import masters

if TYPE_CHECKING:
    from collections.abc import Generator, MutableMapping

#: Every variable an account root can arrive in, read off the register that
#: defines the roots rather than listed again here. A root added there is
#: masked by that addition alone, which is the whole point: the protection
#: cannot be forgotten by the next person to declare a provider.
ROOT_VARIABLES = frozenset(field.env for root in masters.ROOTS.values() for field in root.fields)

#: The rest of what `mise.toml` materializes: the passphrase that decrypts
#: every stack's config, and the backend URL, which names a client key. They
#: are not account-root fields, so the register above does not carry them, and
#: they are secrets on exactly the same terms. `mise.toml` lists them under
#: `redactions`; `test_root_credentials.py` holds that list and this set
#: together.
BACKEND_VARIABLES = frozenset({'PULUMI_CONFIG_PASSPHRASE', 'PULUMI_BACKEND_URL'})

#: What `strip` removes and `fake` will stand in for.
MASKED = ROOT_VARIABLES | BACKEND_VARIABLES


def strip(environment: MutableMapping[str, str]) -> frozenset[str]:
    """Remove every masked variable from `environment`, and name what was there.

    A variable that was not set is not an error -- the operator's workstation
    holds a different set of these from CI, and a suite may not depend on
    which. The return value exists so that a caller can say what it took;
    nothing depends on the values, which are not returned at all.
    """
    removed = frozenset(name for name in MASKED if name in environment)
    for name in removed:
        del environment[name]
    return removed


def fake(name: str) -> str:
    """The stand-in value for `name`.

    Derived from the variable rather than a literal shared between suites, so
    that a value showing up in a failing assertion says both which credential
    the suite was standing in for and that it opens nothing. A name this
    module does not mask is refused: a suite setting one would be relying on a
    protection that is not there.
    """
    if name not in MASKED:
        raise ValueError(f'{name} carries no credential this module masks')
    return f'a-fake-{name.lower().replace("_", "-")}-that-opens-nothing'


@contextmanager
def fake_credentials(*names: str) -> Generator[dict[str, str]]:
    """Hold a fake value in each of `names` for the length of the block.

    Yields what it set, so that a suite can assert against the value the code
    under test received without spelling it a second time.
    """
    values = {name: fake(name) for name in names}
    with pytest.MonkeyPatch.context() as patch:
        for name, value in values.items():
            patch.setenv(name, value)
        yield values
