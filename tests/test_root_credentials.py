"""The masking that keeps the operator's credentials out of this process.

Two halves, and they fail for different reasons. The set of variables is
derived rather than listed, so the cases about it are about a derivation that
could stop covering something; the removal itself is a statement about the
process the suite is running in, which is why one case reads `os.environ`
directly rather than a copy of it.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest
import root_credentials

from kluster.scripts.credentials import masters

MISE = Path(__file__).parent.parent / 'mise.toml'


def test_this_process_holds_none_of_the_operators_credentials() -> None:
    """The property the whole mechanism exists for, asserted where it matters.

    `mise.toml` materializes the account-root token for every process it
    starts, so on an operator workstation this fails the moment `conftest`
    stops stripping -- and it fails here, naming the variable, rather than in
    whichever suite next declares a provider and renders the value into a
    failed assertion.
    """
    present = sorted(name for name in root_credentials.MASKED if name in os.environ)

    assert present == [], 'a credential reached the test process'


def test_stripping_takes_the_credentials_and_leaves_the_rest() -> None:
    """What `strip` does to a mapping, held away from the real environment.

    The second half is as load-bearing as the first: an environment stripped
    of `PATH` or `HOME` would break every suite that starts a subprocess, so
    the mask has to be a named set rather than a heuristic over names that
    look secret.
    """
    environment = {'GITHUB_TOKEN': 'ghp_a_live_looking_token', 'PATH': '/usr/bin', 'HOME': '/home/nobody'}

    removed = root_credentials.strip(environment)

    assert removed == frozenset({'GITHUB_TOKEN'})
    assert environment == {'PATH': '/usr/bin', 'HOME': '/home/nobody'}


def test_stripping_an_environment_that_holds_nothing_is_not_an_error() -> None:
    """CI and a workstation hold different subsets, and no suite may depend on which."""
    environment = {'PATH': '/usr/bin'}

    assert root_credentials.strip(environment) == frozenset()
    assert environment == {'PATH': '/usr/bin'}


def test_every_field_of_every_account_root_is_masked() -> None:
    """The mask is read off the register of roots, so a new root is covered by being declared.

    A literal list here instead would be a second place to remember, and the
    one that is forgotten is the one that leaks: the register is where a root
    is added, and nobody adding one goes looking for the test suite's copy.
    """
    declared = {field.env for root in masters.ROOTS.values() for field in root.fields}

    assert declared <= root_credentials.MASKED
    # Non-empty, so that a register this stopped being able to read could not
    # satisfy the line above by covering nothing.
    assert 'GITHUB_TOKEN' in declared


def test_every_variable_mise_redacts_is_masked() -> None:
    """`mise.toml` says which of the values it materializes are secrets; all of them are masked.

    That file is the reason a credential is in the environment at all, so its
    own list is the outside check on this module's: a secret added to it
    without being masked here is a secret a failing assertion can print.
    """
    redacted = set(tomllib.loads(MISE.read_text())['redactions'])

    assert redacted <= root_credentials.MASKED


def test_a_fake_credential_names_the_variable_it_stands_in_for() -> None:
    """A value that turns up in a diff should identify itself, and say it opens nothing."""
    assert root_credentials.fake('GITHUB_TOKEN') == 'a-fake-github-token-that-opens-nothing'


def test_a_name_that_carries_no_credential_cannot_be_faked() -> None:
    """Asking for a fake under an unmasked name is relying on a protection that is not there."""
    with pytest.raises(ValueError, match='PATH'):
        _ = root_credentials.fake('PATH')


def test_a_fake_credential_lasts_exactly_as_long_as_the_block() -> None:
    """A suite that asks for one does not hand it to whatever runs next.

    Both assertions compare through a name rather than inline, so that a
    failure reports a boolean instead of the value the environment was
    holding. Whatever a broken mask let through would otherwise be printed by
    the case whose subject is that nothing gets printed.
    """
    with root_credentials.fake_credentials('GITHUB_TOKEN') as values:
        matches = os.environ.get('GITHUB_TOKEN') == values['GITHUB_TOKEN']
        assert matches, 'the block did not put its fake value in place'

    present = sorted(name for name in root_credentials.MASKED if name in os.environ)
    assert present == [], 'the block outlived itself'
