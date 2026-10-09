"""The `credentials` command tree: walked to every leaf a caller can run, and read off a message.

Shared by the suites that hold the tree against something else: the dispatch
test drives each leaf through `main`, the slot map's test holds each
`credentials derived` command a row names against the leaves that exist, and
the help suite renders every parser `tree` reaches, the inner ones included.
The walks read the real parser, so a subcommand added later is in every suite
that walks it without anyone editing one.

A refusal or a prompt names its remedy as a `credentials …` command in a code
span. `named_commands` parses each with the CLI's own parser, and
`named_leaves` reads off what each runs, so a case that reads a message here
fails where the message names a leaf the tree no longer carries, rather than
matching a literal that the same formula rebuilt. `parse` is that reading for
a command a case has found some other way, as the help suite finds the ones
the hand-written bring-up order runs.
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


def tree(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Every parser under `parser`, itself and the inner ones included, each with the words that reach it."""
    yield path, parser
    subparsers = _subparsers(parser)
    if subparsers is not None:
        for name, child in subparsers.choices.items():
            yield from tree(child, (*path, name))


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


def parse(argv: list[str], parser: argparse.ArgumentParser | None = None) -> NamedCommand:
    """`argv`, the words after the program name, as the CLI's parser reads them; one it refuses fails the case.

    The failure names the command and the parser's own error line: a renamed
    leaf, a row the tree no longer carries, an option the leaf does not take.
    So does a command the parser exits on without an error (a `--help`), which
    runs nothing. `parser` is built here when the caller has none.

    An option is read only as spelled whole: abbreviation is turned off on
    every parser of the tree before it reads, so a message quoting `--onl`
    for `--only` fails here. The CLI itself accepts such a prefix, until a
    sibling option makes it ambiguous and a message that named it stops
    working.
    """
    parser = parser or cli.build_parser()
    for _, node in tree(parser):
        node.allow_abbrev = False
    complaint = io.StringIO()
    try:
        with contextlib.redirect_stderr(complaint), contextlib.redirect_stdout(io.StringIO()):
            parsed = parser.parse_args(argv)
    except SystemExit as exited:
        lines = complaint.getvalue().strip().splitlines()
        error = lines[-1] if lines else f'it exits with status {exited.code}'
        pytest.fail(f'`credentials {" ".join(argv)}` is not a command the parser carries: {error}')
    return NamedCommand(argv, vars(parsed))


def named_commands(message: str) -> list[NamedCommand]:
    """Every `credentials …` command `message` names, in order, each read by `parse`.

    The parser is built at the call, so where the tree follows a setting --
    the backup generations' rows follow the age pin -- it is the tree in force
    when the message was made.
    """
    parser = cli.build_parser()
    return [parse(argv, parser) for argv in quoted_commands(message)]


#: A leaf as the parser reads it and `cli.main` dispatches on it: the subject,
#: the member under it and the action. A leaf one level up -- `kit bootstrap`,
#: `derived sync` -- carries its own name as the action, which its parser sets.
Leaf = tuple[str, str, str]


def named_leaves(message: str) -> list[Leaf]:
    """The leaf each `credentials …` command `message` names runs, in order, as `named_commands` parses it.

    The leaf alone: an option and its value are left out, so a row named only
    as an option's value (`derived sync --only <row>`) is not taken for the
    row the command runs.
    """
    return [(args['subject'], args['member'], args['action']) for _, args in named_commands(message)]
