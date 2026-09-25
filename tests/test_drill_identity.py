"""The drill age identity: drawn here, one half to the ops-repo Environment, one half to a file.

Against a `gh` that runs nothing and remembers what it was handed, and the
real `age-keygen`, because the property under test is the pairing: what the
Environment holds opens what the recipient on file encrypts to. The three
things a silent change would cost are held here -- a second generation over a
key in service, a push that did not land leaving a committed recipient nobody
holds the key for, and the private half reaching a file or a log line.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest
from fake_gh import RecordedGh

from kluster import conventions
from kluster.scripts.credentials import age, derived
from kluster.scripts.credentials.github_secrets import Forge
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.state_backend import config

pytestmark = pytest.mark.skipif(shutil.which(age.KEYGEN) is None, reason='age-keygen is not on PATH (mise x -- ...)')

#: For the cases that read a recipient on file back: `age` is what parses it
#: (`config.drill_recipient`), where `age-keygen` alone draws the identity.
needs_age = pytest.mark.skipif(shutil.which(age.BINARY) is None, reason='age is not on PATH (mise x -- ...)')

OPS_REPOSITORY = conventions.forge.OPS.full_name
DRILL_ENVIRONMENT = conventions.forge.DRILL.name


@pytest.fixture
def gh() -> RecordedGh:
    return RecordedGh()


@pytest.fixture
def forge(gh: RecordedGh) -> Forge:
    return Forge(token='admin-token', run=gh)


@pytest.fixture
def recipient_file(tmp_path: Path) -> Path:
    return tmp_path / config.DRILL_RECIPIENT


def _pushed(gh: RecordedGh) -> str:
    """The one value the fake was handed for the drill's slot."""
    (value,) = [value for (_, _, name), value in gh.values.items() if name == 'DRILL_AGE_IDENTITY']
    return value


@needs_age
def test_the_pushed_secret_and_the_written_recipient_are_one_pair(
    forge: Forge, gh: RecordedGh, recipient_file: Path
) -> None:
    written = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    # The pairing is proven by the tool that defines it, not by trusting the
    # generator to have written the two halves of the same key.
    assert age.recipient(_pushed(gh)) == written
    assert config.drill_recipient(recipient_file) == written


def test_the_private_half_lands_in_the_ops_repo_drill_environment_under_its_name(
    forge: Forge, gh: RecordedGh, recipient_file: Path
) -> None:
    # The address is the one the slot map advertises for the row, and it is
    # the ops repository's Environment rather than the deployment repository's
    # -- a `drill` Environment exists in exactly one of them (ci.md §3).
    _ = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    assert list(gh.values) == [(OPS_REPOSITORY, DRILL_ENVIRONMENT, 'DRILL_AGE_IDENTITY')]
    assert ['secret', 'set', 'DRILL_AGE_IDENTITY', '--repo', OPS_REPOSITORY, '--env', DRILL_ENVIRONMENT] in (
        gh.invocations
    )


def test_the_pushed_value_is_the_secret_line_alone(forge: Forge, gh: RecordedGh, recipient_file: Path) -> None:
    # No `age-keygen` comment lines and no trailing newline: a GitHub secret
    # is stored exactly as piped in, and the drill writes it to the identity
    # file `state-backend restore --identity-file` reads.
    _ = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    pushed = _pushed(gh)
    assert pushed.startswith(age.SECRET_PREFIX)
    assert pushed == pushed.strip()
    assert '\n' not in pushed


@needs_age
def test_a_second_generation_is_refused_by_the_recipient_on_file(
    forge: Forge, gh: RecordedGh, recipient_file: Path
) -> None:
    # The row is no escrow label, so nothing counts its generations: the
    # committed public half is the one durable trace of a key in service.
    first = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    with pytest.raises(SlotRefused, match='--rotate'):
        _ = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    # Nothing moved on either side: one push, the first recipient still on file.
    assert len(gh.values) == 1
    assert age.recipient(_pushed(gh)) == first
    assert config.drill_recipient(recipient_file) == first


@needs_age
def test_rotate_replaces_both_halves(forge: Forge, gh: RecordedGh, recipient_file: Path) -> None:
    first = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    second = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=True)

    # One slot: overwriting the Environment secret is deleting the old key,
    # and the file names the successor alone.
    assert second != first
    assert age.recipient(_pushed(gh)) == second
    assert config.drill_recipient(recipient_file) == second


def test_rotate_with_nothing_on_file_is_refused(forge: Forge, gh: RecordedGh, recipient_file: Path) -> None:
    # An operator who believes a key is in service where none is would skip
    # the converge that installs the recipient.
    with pytest.raises(SlotRefused, match='names no drill recipient'):
        _ = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=True)

    assert gh.values == {}
    assert not recipient_file.exists()


def _mistyped(public: str) -> str:
    """`public` with its last six characters replaced: the prefix survives and the checksum does not."""
    return f'{public[:-6]}{"qqqqqq" if not public.endswith("qqqqqq") else "pppppp"}'


def _refused_files() -> list[str]:
    """Recipient files the reader refuses: a hand edit that broke the checksum, and a rotation done by hand."""
    return [
        f'# The drill age identity, public half.\n{_mistyped(age.generate().public)}\n',
        f'{age.generate().public}\n{age.generate().public}\n',
    ]


@needs_age
@pytest.mark.parametrize('shape', range(2), ids=['bad-checksum', 'two-recipients'])
def test_rotate_draws_a_successor_over_a_file_the_reader_refuses(
    shape: int, forge: Forge, gh: RecordedGh, recipient_file: Path
) -> None:
    # The only repair there is: the public half cannot be recomputed from an
    # Environment secret nobody can read back, so `--rotate` asks whether the
    # file is there and never parses it.
    _ = recipient_file.write_text(_refused_files()[shape])

    written = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=True)

    assert age.recipient(_pushed(gh)) == written
    assert config.drill_recipient(recipient_file) == written


@needs_age
@pytest.mark.parametrize('shape', range(2), ids=['bad-checksum', 'two-recipients'])
def test_a_file_the_reader_refuses_names_rotate_as_its_repair(
    shape: int, forge: Forge, gh: RecordedGh, recipient_file: Path
) -> None:
    # Without `--rotate` the file is read, and a refusal that stopped at "not
    # an age recipient" would leave deleting the file as the only way out that
    # anyone finds. Nothing is drawn, and the line is not repeated.
    content = _refused_files()[shape]
    _ = recipient_file.write_text(content)

    with pytest.raises(SlotRefused, match='`--rotate` draws a successor') as refused:
        _ = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    assert gh.values == {}
    assert recipient_file.read_text() == content
    for line in content.splitlines():
        if not line.startswith('#'):
            assert line not in str(refused.value)


def test_a_push_that_does_not_land_leaves_the_file_untouched(gh: RecordedGh, recipient_file: Path) -> None:
    # A committed recipient whose private half never landed is a dump
    # encrypted to a key nobody holds, found a quarter later; an Environment
    # secret with no recipient on file costs a re-run.
    gh.forgets = True

    with pytest.raises(SlotRefused, match='listing does not show it'):
        _ = derived.drill_age_identity(Forge(token='admin-token', run=gh), recipient_file=recipient_file, rotate=False)

    assert not recipient_file.exists()


def test_the_private_half_reaches_no_file_and_no_log_line(
    forge: Forge, gh: RecordedGh, recipient_file: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Process, `gh` standard input, Environment -- and nowhere else.

    The file the generator writes is the only file it touches, and the log at
    the level an operator runs it at names the slot and the file without the
    value. Both are checked against the secret the fake was actually handed,
    so a leak of any part of it is caught rather than one particular spelling.
    """
    caplog.set_level(logging.INFO)

    _ = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    secret = _pushed(gh)
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert secret not in path.read_text(), path
    assert secret not in caplog.text
    assert 'DRILL_AGE_IDENTITY' in caplog.text


def test_the_written_file_is_one_recipient_with_the_rest_comments(forge: Forge, recipient_file: Path) -> None:
    # The reader that every line-per-value file here goes through treats `#`
    # lines as the author's, so what the file says about itself costs the
    # recipient list nothing.
    written = derived.drill_age_identity(forge, recipient_file=recipient_file, rotate=False)

    lines = recipient_file.read_text().splitlines()
    assert [line for line in lines if not line.startswith('#')] == [written]
    assert recipient_file.read_text().endswith('\n')
