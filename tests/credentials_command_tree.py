"""The `credentials` command tree, walked to every leaf a caller can run.

Shared by the suites that hold the tree against something else: the dispatch
test drives each leaf through `main`, and the slot map's test holds each
`credentials derived` command a row names against the leaves that exist. The
walk reads the real parser, so a subcommand added later is in every suite
that walks it without anyone editing one.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path
from typing import cast

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
