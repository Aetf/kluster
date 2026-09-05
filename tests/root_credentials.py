"""What a test process may see of the operator's credentials in its environment: nothing.

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

**The environment is one of three channels, and this module closes only it.**
`masters._find` consults the desktop secret store, then a file under the
checkout's own `.credentials/`, and only then the variable; `workstation`
resolves that directory from `__file__`, so on an operator workstation the
file layer answers with live material without the environment being involved
at all. Closing those two is not the same problem: they are read at *test*
time and never at import, so a fixture reaches them where it could not reach
this, and until one exists a suite that touches `masters`, `workstation` or
`kdbx` redirects `workstation.directory` at a `tmp_path` itself, as
`tests/test_masters.py::local` does. `docs/framework/testing.md` §1.1 is where
that discipline is written down.

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

#: The other two variables `mise.toml` lists under `redactions`: the
#: passphrase that decrypts every stack's config, and the backend URL, which
#: names a client key. They are not account-root fields, so the register above
#: does not carry them, and they are secrets on exactly the same terms.
BACKEND_VARIABLES = frozenset({'PULUMI_CONFIG_PASSPHRASE', 'PULUMI_BACKEND_URL'})

#: What `strip` removes and `fake` will stand in for.
MASKED = ROOT_VARIABLES | BACKEND_VARIABLES

#: Variables that carry a *path* to credential material rather than the
#: material itself, and are therefore deliberately left in place: a failing
#: assertion renders the path, and the tests that need the files behind them
#: point the path elsewhere instead. Named rather than merely absent so that
#: `test_root_credentials.py` can hold every key `mise.toml` sets to a
#: deliberate verdict -- masked, or knowingly not -- rather than to silence.
UNMASKED_PATHS = frozenset({'PGSSLROOTCERT', 'PGSSLCERT', 'PGSSLKEY', 'KLUSTER_KDBX'})


def strip(environment: MutableMapping[str, str]) -> None:
    """Remove every masked variable from `environment`.

    A variable that was not set is not an error -- the operator's workstation
    holds a different set of these from CI, and a suite may not depend on
    which. Nothing is reported back, in either direction: a caller that asked
    what was there would be holding the answer this exists to destroy.
    """
    for name in MASKED:
        environment.pop(name, None)


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
