"""Every `--help` in the credentials tree explains itself, without the register open.

An operator who runs `credentials <something> --help` may have no checkout, no
browser and no second window. So a help text states the mechanism it is talking
about, and a pointer to a document is allowed only as a trailing `See also`
line: something a reader can ignore without losing the explanation.

The rule enforced here, in full, and deliberately small:

-   A line of rendered help **mentions a document** when it contains `§` or
    `.md`. That catches a register section (`§2.2`), a document name
    (`credentials.md`) and a path to one (`physical/gateway.md`), and nothing
    else in this tree is spelled either way.
-   In each rendered `--help`, from the first line that mentions a document
    onwards, **every non-blank line must begin with `See also:`**. One
    condition covers both halves of the rule: a mention that is not itself a
    `See also` line fails it, and so does a `See also` line with anything but
    more of them after it. Since argparse prints the epilog last, "trailing" is
    then the same thing as "in the epilog", which is where `cli._see_also` puts
    it.

The second property here is about the one help text that is *not* generated
from a register: the bring-up order the top-level epilog carries (`cli._ORDER`)
is written out by hand, so a row added to a register reaches the tree on its
own but reaches that order only when someone types it in. The walk below is
what says so, for the register whose rows are all run the same way.

What this cannot check is whether the prose is any good: a help text that says
nothing at all passes, and one that leans on jargon passes. Comprehensibility
is a reviewer's job. The mechanical part -- that no explanation has been
replaced by a citation, and that the order names every row it walks -- is this
file's.

Rendering happens at a fixed width, because the property is about rendered
lines and argparse wraps to the terminal it finds. A `See also` line is short
enough to survive any width a person reads help at; fixing the width keeps the
test from depending on the one it is run in.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from typing import cast

import pytest

from kluster.scripts.credentials import cli, devices, escrow

#: The width every help text here is rendered at. Argparse reads `COLUMNS` for
#: its own wrapping, so setting it makes the rendering the same in a test
#: runner, in a terminal and in CI.
WIDTH = '100'

#: A line mentioning a register section or a document file.
MENTIONS = ('§', '.md')

#: What a mention has to be part of, and the only thing allowed after one.
SEE_ALSO = 'See also:'


def _tree(
    parser: argparse.ArgumentParser, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Every parser in the tree, keyed by the words that reach it.

    Recursive because the tree is generated from the registers: a row added to
    one of them arrives here without anyone editing this file, which is the
    only way an enforcement test stays true.
    """
    yield path, parser
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            subparsers = cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)  # pyright: ignore[reportPrivateUsage]
            for name, sub in subparsers.choices.items():
                yield from _tree(sub, (*path, name))


def _parsers() -> dict[str, argparse.ArgumentParser]:
    """The whole tree, keyed by the command line that reaches each parser."""
    return {' '.join(('credentials', *path)): parser for path, parser in _tree(cli.build_parser())}


def commands() -> list[str]:
    """One test case per parser."""
    return list(_parsers())


def _rendered(name: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv('COLUMNS', WIDTH)
    return _parsers()[name].format_help().splitlines()


def _mentions(line: str) -> bool:
    return any(token in line for token in MENTIONS)


def _from_the_first_mention(lines: list[str]) -> list[str]:
    """The non-blank lines that have to be `See also` lines, if there are any."""
    first = next((index for index, line in enumerate(lines) if _mentions(line)), None)
    return [] if first is None else [line for line in lines[first:] if line.strip()]


@pytest.mark.parametrize('name', commands())
def test_a_document_is_mentioned_only_in_a_trailing_see_also(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for line in _from_the_first_mention(_rendered(name, monkeypatch)):
        assert line.strip().startswith(SEE_ALSO), (
            f'`{name} --help` mentions a document outside a trailing `{SEE_ALSO}` line: {line.strip()!r}. '
            'A help text explains its own mechanism; the reference goes in the epilog, last, and only there.'
        )


def test_the_tree_really_does_carry_see_also_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    # The rule above is satisfied by a tree that mentions nothing at all, so
    # this asserts the shape being enforced is one the tree actually uses.
    every_line = [line for name in commands() for line in _rendered(name, monkeypatch)]

    assert [line for line in every_line if line.strip().startswith(SEE_ALSO)]


def console_rows() -> list[str]:
    """Every row a bring-up delivers by printing console steps and taking a value.

    Two registers hold them and they are run the same way: the device rows,
    whose value goes into a stack's config, and the escrowed rows nothing here
    can draw, whose value goes into the registry.
    """
    return [
        *devices.DEVICES,
        *(name for name, label in escrow.rows().items() if isinstance(label.origin, escrow.Console)),
    ]


@pytest.mark.parametrize('member', console_rows())
def test_the_bring_up_order_names_every_row_made_in_a_console(member: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every one of these is delivered the same way and belongs to the same
    # stage of a bring-up, so an operator following the order and a tree built
    # from those registers have to agree about how many there are.
    order = _rendered('credentials', monkeypatch)

    assert any(f'credentials derived {member} record' in line for line in order), (
        f'`{member}` is a row made in a console but the bring-up order never runs it: '
        'an operator who follows that order would finish with the credential undelivered.'
    )


def test_a_mention_in_the_middle_of_a_help_text_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    # The check itself, against a parser built to break it: a description is
    # rendered before the options, so a reference in one can never be trailing.
    monkeypatch.setenv('COLUMNS', WIDTH)
    offender = argparse.ArgumentParser(prog='offender', description='what this does is written in §4.1.')

    caught = _from_the_first_mention(offender.format_help().splitlines())

    assert caught
    assert not all(line.strip().startswith(SEE_ALSO) for line in caught)
