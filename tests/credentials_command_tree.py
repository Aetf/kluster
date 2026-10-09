"""The `credentials` command tree: walked to every leaf a caller can run, and read off a message.

Shared by the suites that hold the tree against something else: the dispatch
test drives each leaf through `main`, and the slot map's test holds each
`credentials derived` command a row names against the leaves that exist. The
walk reads the real parser, so a subcommand added later is in every suite
that walks it without anyone editing one.

A refusal or a prompt names its remedy as a `credentials …` command in a code
span. `named_commands` parses each with the CLI's own parser, so a case that
reads a message here fails where the message names a leaf the tree no longer
carries, rather than matching a literal that the same formula rebuilt.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest

from kluster.scripts.credentials import cli


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:  # pyright: ignore[reportPrivateUsage]
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            return cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)  # pyright: ignore[reportPrivateUsage]
    return None


def _fill(parser: argparse.ArgumentParser, into: list[str]) -> None:
    """Append whatever this level insists on, so a leaf can be reached at all.

    Values are placeholders: a caller that runs a leaf stubs its handler, so
    only the dispatch sees them. Required-ness is read off the parser rather
    than listed here, which is what keeps a newly required option from
    silently going untested.
    """
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction | argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
            continue
        placeholder = 'placeholder.kdbx' if action.type is Path else 'placeholder'
        if action.option_strings:
            if action.required:
                into.extend((action.option_strings[0], placeholder))
        elif action.nargs not in ('?', '*'):
            into.append(placeholder)


def leaves(parser: argparse.ArgumentParser) -> Iterator[list[str]]:
    """Every command that can actually be run, as the argv that runs it."""
    prefix: list[str] = []
    _fill(parser, prefix)
    subparsers = _subparsers(parser)
    if subparsers is None:
        yield prefix
        return
    for name, child in subparsers.choices.items():
        for tail in leaves(child):
            yield [*prefix, name, *tail]


def commands() -> list[list[str]]:
    """Every leaf of `credentials`, each as the shortest argv that reaches it."""
    return list(leaves(cli.build_parser()))


#: A `credentials …` command where prose names one, in a code span.
QUOTED = re.compile(r'`credentials ([^`]+)`')


def quoted_commands(message: str) -> list[list[str]]:
    """Every `credentials …` command `message` names in a code span, in order, as argv without the program name.

    Words and nothing more. Whether each is a command the tree carries is
    `named_commands`' to hold; a caller reads the words alone only where a
    command may rightly be absent from the tree, as an unbuilt row's is.
    """
    return [command.split() for command in QUOTED.findall(message)]


class NamedCommand(NamedTuple):
    """One command a message names: its words, and what the CLI's parser reads them as."""

    #: The words after the program name, as the message spells them.
    argv: list[str]
    #: The parsed namespace, by `dest`: `subject`, `member`, `action` and each option.
    args: dict[str, Any]


def named_commands(message: str) -> list[NamedCommand]:
    """Every `credentials …` command `message` names, in order, each parsed by the CLI's own parser.

    One the parser refuses fails the calling case, naming the command and the
    parser's own error line: a renamed leaf, a row the tree no longer carries,
    an option the leaf does not take. So does one the parser exits on without
    an error (a `--help`), which runs nothing. The parser is built at the call,
    so where the tree follows a setting -- the backup generations' rows follow
    the age pin -- it is the tree in force when the message was made.
    """
    parser = cli.build_parser()
    named: list[NamedCommand] = []
    for argv in quoted_commands(message):
        complaint = io.StringIO()
        try:
            with contextlib.redirect_stderr(complaint), contextlib.redirect_stdout(io.StringIO()):
                parsed = parser.parse_args(argv)
        except SystemExit as exited:
            lines = complaint.getvalue().strip().splitlines()
            error = lines[-1] if lines else f'it exits with status {exited.code}'
            pytest.fail(f'`credentials {" ".join(argv)}` is not a command the parser carries: {error}')
        named.append(NamedCommand(argv, vars(parsed)))
    return named
