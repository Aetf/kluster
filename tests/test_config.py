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


def test_strings_keeps_a_list_of_strings() -> None:
    assert config.strings(['alice', 'bob'], 'the alert recipients') == ('alice', 'bob')


def test_strings_names_the_value_that_is_not_a_list() -> None:
    with pytest.raises(TypeError, match='the alert recipients must be a list, not str'):
        _ = config.strings('alice', 'the alert recipients')


def test_strings_names_the_value_that_holds_an_empty_entry() -> None:
    with pytest.raises(TypeError, match='the alert recipients must be a list of non-empty strings'):
        _ = config.strings(['alice', ''], 'the alert recipients')


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

    with pytest.raises(ValueError, match='the operator keys: .* holds no values'):
        _ = config.lines(keys, 'the operator keys')
