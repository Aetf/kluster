"""What `credentials derived github-<app>-key record` tells the operator about each App.

The steps are printed at the moment a key is asked for, and the operator
follows them: an App created from them is installed where they say and named
to its workflow the way they say. So the two facts that belong to the census
rather than to the console -- which repositories the App is installed on, and
where its client id is recorded -- are held to the census here, for each App
the register holds a key of.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

import pytest

from kluster import conventions
from kluster.scripts.credentials import escrow


class AppKey(NamedTuple):
    """One App key the register holds, and the census entries it answers to."""

    label: str
    app: conventions.forge.App
    #: The variable that carries the App's client id.
    variable: conventions.forge.Variable
    #: What the key signs for, as the label's description spells it.
    permission: str


APP_KEYS = [
    pytest.param(
        AppKey(
            escrow.DISPATCH_KEY,
            conventions.forge.DISPATCH_APP,
            conventions.forge.DISPATCH_APP_CLIENT_ID,
            'contents:write',
        ),
        id='dispatch',
    ),
    pytest.param(
        AppKey(
            escrow.TRIGGER_KEY,
            conventions.forge.TRIGGER_APP,
            conventions.forge.TRIGGER_APP_CLIENT_ID,
            'actions:write',
        ),
        id='trigger',
    ),
]


def printed_steps(caplog: pytest.LogCaptureFixture, label: str) -> str:
    """The steps `record` prints for `label`, whitespace folded so a wrapped phrase reads whole."""
    with caplog.at_level(logging.INFO):
        escrow.announce(label)
    return ' '.join(' '.join(record.getMessage() for record in caplog.records).split())


def listed(phrase: str) -> set[str]:
    """The names in a sentence's list: `a`, `a and b`, `a, b and c`."""
    return set(re.split(r', | and ', phrase))


def installations(app: conventions.forge.App) -> set[str]:
    """Every repository the census installs `app` on."""
    return {repository.name for repository in conventions.forge.REPOSITORIES if app in repository.apps}


@pytest.mark.parametrize('key', APP_KEYS)
def test_the_steps_install_the_app_on_every_repository_the_census_names(
    caplog: pytest.LogCaptureFixture, key: AppKey
) -> None:
    # An App installed on fewer repositories than its workflows mint for is a
    # mint that fails at the run -- on the repository the steps left out.
    steps = printed_steps(caplog, key.label)
    installed = re.search(r'installed on (.+?) and on nothing else', steps)

    assert installations(key.app)
    assert installed is not None
    assert listed(installed.group(1)) == installations(key.app)


@pytest.mark.parametrize('key', APP_KEYS)
def test_the_label_names_every_repository_its_key_signs_for(key: AppKey) -> None:
    what = escrow.register()[key.label].what
    signs_for = re.search(rf'{re.escape(key.permission)} on (.+)$', what)

    assert signs_for is not None
    assert listed(signs_for.group(1)) == installations(key.app)


@pytest.mark.parametrize('key', APP_KEYS)
def test_the_steps_send_the_client_id_to_the_census_not_the_page(caplog: pytest.LogCaptureFixture, key: AppKey) -> None:
    # The client id is recorded in `conventions.forge` and declared as a
    # repository variable by the `github` stack; a step that sends the
    # operator to copy it off the page by hand is a second, unrecorded copy.
    steps = printed_steps(caplog, key.label)

    # The pointer is resolved, so a census entry renamed under it fails here.
    (pointer,) = re.findall(r'`conventions\.([\w.]+)`', steps)
    named: object = conventions
    for part in pointer.split('.'):
        named = getattr(named, part)
    assert named is key.app, pointer
    assert key.variable.value == key.app.client_id
    assert 'read off the page' not in steps

    # Named, and on the repositories the forge declares it on: the operator
    # landing a new client id looks for it there.
    declared = re.search(
        rf'the ([\w-]+(?:(?:, | and )[\w-]+)*) repository variable `{re.escape(key.variable.name)}`', steps
    )
    assert declared is not None
    assert listed(declared.group(1)) == {
        repository.name for repository in conventions.forge.REPOSITORIES if key.variable in repository.variables
    }
