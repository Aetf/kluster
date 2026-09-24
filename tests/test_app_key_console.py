"""What `credentials derived github-dispatch-key record` tells the operator about the App.

The steps are printed at the moment a key is asked for, and the operator
follows them: an App created from them is installed where they say and named
to its workflow the way they say. So the two facts that belong to the census
rather than to the console -- which repositories the App is installed on, and
where its client id is recorded -- are held to the census here.
"""

from __future__ import annotations

import logging
import re

import pytest

from kluster import conventions
from kluster.scripts.credentials import escrow


def printed_steps(caplog: pytest.LogCaptureFixture, label: str) -> str:
    """The steps `record` prints for `label`, whitespace folded so a wrapped phrase reads whole."""
    with caplog.at_level(logging.INFO):
        escrow.announce(label)
    return ' '.join(' '.join(record.getMessage() for record in caplog.records).split())


def listed(phrase: str) -> set[str]:
    """The names in a sentence's list: `a`, `a and b`, `a, b and c`."""
    return set(re.split(r', | and ', phrase))


def installations() -> set[str]:
    """Every repository the census installs the dispatch App on."""
    return {
        repository.name
        for repository in conventions.forge.REPOSITORIES
        if conventions.forge.DISPATCH_APP in repository.apps
    }


def test_the_dispatch_steps_install_the_app_on_every_repository_the_census_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An App installed on fewer repositories than its workflows mint for is a
    # mint that fails at the run -- on the repository the steps left out.
    steps = printed_steps(caplog, escrow.DISPATCH_KEY)
    installed = re.search(r'installed on (.+?) and on nothing else', steps)

    assert installed is not None
    assert listed(installed.group(1)) == installations()


def test_the_dispatch_label_names_every_repository_its_key_signs_for() -> None:
    what = escrow.register()[escrow.DISPATCH_KEY].what
    signs_for = re.search(r'contents:write on (.+)$', what)

    assert signs_for is not None
    assert listed(signs_for.group(1)) == installations()


def test_the_dispatch_steps_send_the_client_id_to_the_census_not_the_page(caplog: pytest.LogCaptureFixture) -> None:
    # The client id is recorded in `conventions.forge` and declared as a
    # repository variable by the `github` stack; a step that sends the
    # operator to copy it off the page by hand is a second, unrecorded copy.
    steps = printed_steps(caplog, escrow.DISPATCH_KEY)

    assert '`conventions.forge.DISPATCH_APP`' in steps
    assert f'`{conventions.forge.DISPATCH_APP_CLIENT_ID.name}`' in steps
    assert 'read off the page' not in steps
