"""The age wrapper is checked against age itself.

There is nothing to unit-test here in isolation: the module is a shell around
the tool that owns the format, so what is worth asserting is that the round
trip works, that a wrong key is a refusal rather than a wrong plaintext, and
that the pin the appliance downloads is the pin this suite exercises.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kluster.scripts.credentials import age

age_binary = shutil.which(age.BINARY)
needs_age = pytest.mark.skipif(age_binary is None, reason='age is not on PATH (mise x -- ...)')


@needs_age
def test_an_identity_round_trips(tmp_path: Path) -> None:
    identity = age.generate()
    path = tmp_path / 'secret.age'

    _ = path.write_text(age.encrypt('hunter2', [identity.public]))

    assert age.decrypt(path, [identity.secret]) == 'hunter2'


@needs_age
def test_the_public_half_is_the_one_age_computes() -> None:
    identity = age.generate()

    assert identity.secret.startswith(age.SECRET_PREFIX)
    assert identity.public.startswith(age.PUBLIC_PREFIX)
    assert age.recipient(identity.secret) == identity.public


@needs_age
def test_two_identities_are_two_identities() -> None:
    assert age.generate().public != age.generate().public


@needs_age
def test_a_file_is_armoured_and_recognised_as_one(tmp_path: Path) -> None:
    # The escrow's `check` runs without a key, so "is this an age file at all"
    # has to be answerable from the bytes.
    armoured = age.encrypt('x', [age.generate().public])

    assert age.is_armoured(armoured)
    assert not age.is_armoured('-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----')


@needs_age
def test_any_of_several_identities_opens_it(tmp_path: Path) -> None:
    # What makes a re-wrap resumable: the caller does not have to know which
    # key a given file is currently under.
    wanted = age.generate()
    other = age.generate()
    path = tmp_path / 'secret.age'
    _ = path.write_text(age.encrypt('hunter2', [wanted.public]))

    assert age.decrypt(path, [other.secret, wanted.secret]) == 'hunter2'


@needs_age
def test_the_wrong_identity_is_a_refusal(tmp_path: Path) -> None:
    path = tmp_path / 'secret.age'
    _ = path.write_text(age.encrypt('hunter2', [age.generate().public]))

    with pytest.raises(age.AgeError):
        _ = age.decrypt(path, [age.generate().secret])


@needs_age
def test_multiple_recipients_all_open_it(tmp_path: Path) -> None:
    # The generational pair the appliance encrypts every dump to.
    current = age.generate()
    previous = age.generate()
    path = tmp_path / 'dump.age'
    _ = path.write_text(age.encrypt('backup', [current.public, previous.public]))

    assert age.decrypt(path, [current.secret]) == 'backup'
    assert age.decrypt(path, [previous.secret]) == 'backup'


def test_encrypting_to_nobody_is_refused() -> None:
    with pytest.raises(age.AgeError, match='no recipient'):
        _ = age.encrypt('x', [])


def test_decrypting_with_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(age.AgeError, match='no identity'):
        _ = age.decrypt(tmp_path / 'absent.age', [])


def test_a_missing_tool_names_where_it_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # A headless run without mise's tools should say what to do, not raise a
    # FileNotFoundError from three frames down.
    monkeypatch.setattr(age, 'BINARY', 'age-that-is-not-installed')

    with pytest.raises(age.AgeError, match='mise.toml'):
        _ = age.decrypt(Path('irrelevant'), ['AGE-SECRET-KEY-1'])


def test_age_url_matches_the_pinned_version() -> None:
    """A version bumped without its URL would fetch the old binary and pass
    its own digest check."""
    from kluster.scripts.state_backend import settings

    assert settings.AGE_VERSION in settings.AGE_URL
    assert settings.AGE_URL.endswith('linux-amd64.tar.gz')
    assert len(settings.AGE_SHA256) == 64


def test_local_age_matches_the_appliance_pin() -> None:
    """The round trip is only worth as much as the tool that runs it.

    `mise.toml`'s age is what this suite exercises; the appliance runs the one
    `settings.AGE_VERSION` pins. Letting them drift would leave the
    appliance's version untested.
    """
    import tomllib

    from kluster.scripts.state_backend import settings

    tools = tomllib.loads((Path(__file__).parent.parent / 'mise.toml').read_text())['tools']
    assert f'v{tools["age"]}' == settings.AGE_VERSION
