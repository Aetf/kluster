"""The readers that turn untyped configuration into values, and their refusals.

What is worth holding here is the refusal, not the happy path: the whole
reason these functions exist is that a shape mistake has to name what is wrong
instead of surfacing three frames later as a `KeyError` or, worse, as a
silently empty answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kluster.lib import config


def test_text_keeps_a_non_empty_string() -> None:
    assert config.text('quiet-brook', 'the `state` stack output `password`') == 'quiet-brook'


def test_text_names_the_type_and_never_the_value() -> None:
    """The refusal is logged by the command that was pushing the value."""
    secret = {'ciphertext': 'A-LIVE-SECRET'}

    with pytest.raises(TypeError) as refused:
        _ = config.text(secret, 'the `state` stack output `password`')

    # A stack output exported in the wrong shape is still a credential, and
    # `slots.sync` logs what this raises -- so naming the value would put it in
    # the run log of the command whose whole job was to keep it out of one.
    assert 'the `state` stack output `password` must be a non-empty string, and is dict' in str(refused.value)
    assert 'A-LIVE-SECRET' not in str(refused.value)


def test_text_tells_an_empty_string_apart_from_the_wrong_type() -> None:
    # Naming the type rather than the value would otherwise collapse the two
    # into "and is str", which is the one thing an operator cannot act on.
    with pytest.raises(TypeError, match='must be a non-empty string, and is empty'):
        _ = config.text('', 'the `state` stack output `password`')


def test_strings_keeps_a_list_of_strings() -> None:
    assert config.strings(['alice', 'bob'], 'the alert recipients') == ('alice', 'bob')


def test_strings_names_the_value_that_is_not_a_list() -> None:
    with pytest.raises(TypeError, match='the alert recipients must be a list, not str'):
        _ = config.strings('alice', 'the alert recipients')


def test_strings_names_the_entry_that_is_empty_by_its_position() -> None:
    with pytest.raises(
        TypeError, match='the alert recipients must be a list of non-empty strings, and entry 2 is empty'
    ):
        _ = config.strings(['alice', ''], 'the alert recipients')


def test_strings_names_the_entry_of_the_wrong_type_by_its_position() -> None:
    with pytest.raises(TypeError, match='the alert recipients must be a list of non-empty strings, and entry 2 is int'):
        _ = config.strings(['alice', 7], 'the alert recipients')


@pytest.mark.parametrize(
    'value',
    [
        # Beside the entry refused, and inside it: a refusal that quoted the
        # entry it objects to would leak exactly when that entry holds
        # addresses, as a nested list or a mapping written by mistake does.
        ['A-PRIVATE-ADDRESS', ''],
        ['A-PRIVATE-ADDRESS', 7],
        [['A-PRIVATE-ADDRESS']],
        [{'to': 'A-PRIVATE-ADDRESS'}],
        {'to': 'A-PRIVATE-ADDRESS'},
    ],
)
def test_strings_never_prints_a_value(value: object) -> None:
    # The list may be private -- the budget alerts' recipients are -- and
    # whoever reads the configuration logs what this raises.
    with pytest.raises(TypeError) as refused:
        _ = config.strings(value, 'the alert recipients')
    assert 'A-PRIVATE-ADDRESS' not in str(refused.value)


def test_lines_drops_blanks_and_comments(tmp_path: Path) -> None:
    keys = tmp_path / 'operator-keys.txt'
    _ = keys.write_text('# who may log in\n\nssh-ed25519 AAAA alice\n  ssh-ed25519 BBBB bob  \n')

    assert config.lines(keys, 'the operator keys') == ('ssh-ed25519 AAAA alice', 'ssh-ed25519 BBBB bob')


def test_lines_names_the_file_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match='the operator keys: no such file'):
        _ = config.lines(tmp_path / 'operator-keys.txt', 'the operator keys')


def test_lines_refuses_a_file_that_holds_only_comments(tmp_path: Path) -> None:
    """An empty answer here is how a missing key becomes a box nobody can log in to."""
    keys = tmp_path / 'operator-keys.txt'
    _ = keys.write_text('# nobody yet\n\n')

    with pytest.raises(ValueError, match=r'the operator keys: .* holds no values'):
        _ = config.lines(keys, 'the operator keys')
