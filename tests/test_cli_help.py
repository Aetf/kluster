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
own but reaches that order only when someone types it in. The walks below are
what say so: one over every `derived <row> mint` the tree offers, and one over
the rows a person makes, which two registers hold and which are run the same
way. What the order runs is read by the CLI's own parser rather than matched
as text, so a leaf renamed in the tree is one the order no longer runs, even
while its hand-written line still names the old word.

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
import re

import pytest
from credentials_command_tree import Leaf, parse, tree

from kluster.scripts.credentials import cli, derived, devices, escrow

#: The width every help text here is rendered at. Argparse reads `COLUMNS` for
#: its own wrapping, so setting it makes the rendering the same in a test
#: runner, in a terminal and in CI.
WIDTH = '100'

#: A line mentioning a register section or a document file.
MENTIONS = ('§', '.md')

#: What a mention has to be part of, and the only thing allowed after one.
SEE_ALSO = 'See also:'


def _parsers() -> dict[str, argparse.ArgumentParser]:
    """The whole tree, inner parsers included, keyed by the command line that reaches each.

    Walked because the tree is generated from the registers: a row added to
    one of them arrives here without anyone editing this file, which is the
    only way an enforcement test stays true. The walk is `credentials_command_tree`'s
    `tree`, which yields the inner parsers as well as the leaves: an operator
    reads their help too.
    """
    return {' '.join(('credentials', *path)): parser for path, parser in tree(cli.build_parser())}


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


def test_a_sealed_row_says_how_its_value_comes_to_exist_and_how_it_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DKIM key is carried and piped in; the help says so, not what fits a row a person types."""
    row = ' '.join(' '.join(_rendered('credentials derived dkim-exim', monkeypatch)).split())
    listing = ' '.join(' '.join(_rendered('credentials derived', monkeypatch)).split())

    assert 'seal what is piped in' in row
    assert 'carried from the legacy cluster' in row
    assert 'carried from the legacy cluster' in listing
    assert 'made by a person' not in row


def runs_of(row: str, order: list[str]) -> list[Leaf]:
    """Each leaf of `row` the rendered order runs, as the parser reads it: subject, row and verb.

    The order names a command bare on a line of its own, or in a code span in
    a sentence, so it is read off each line as `credentials derived <row>`
    and the lower-case words after it, up to an option, a code span's end or
    the line's. Read by the parser rather than matched as text, so a leaf
    renamed in the tree fails here while the hand-written order still names it.
    """
    command = re.compile(rf'credentials (derived {re.escape(row)}(?![\w-])(?: [a-z][a-z-]*)*)')
    runs: list[Leaf] = []
    for line in order:
        for words in command.findall(line):
            args = parse(words.split()).args
            runs.append((args['subject'], args['member'], args['action']))
    return runs


def mint_rows() -> list[str]:
    """Every row the tree offers a `credentials derived <row> mint` for, found by walking it.

    Walked rather than listed, so a mint added to the tree is a case here
    without anyone editing this file.
    """
    return [
        path[1] for path, _ in tree(cli.build_parser()) if len(path) == 3 and path[0] == 'derived' and path[2] == 'mint'
    ]


def test_the_mint_walk_finds_the_mints() -> None:
    # A walk that found nothing would leave the case below with no
    # parameters, which pytest reports as skipped rather than failed.
    assert derived.OCI_PHYSICAL_ROW in mint_rows()


@pytest.mark.parametrize('row', mint_rows())
def test_when_to_run_what_names_every_mint(row: str, monkeypatch: pytest.MonkeyPatch) -> None:
    order = _rendered('credentials', monkeypatch)

    assert ('derived', row, 'mint') in runs_of(row, order), (
        f'`credentials derived {row} mint` is in the tree but the order in `credentials --help` never runs '
        'it: an operator who follows that order would finish with the credential unminted.'
    )


def console_rows() -> list[str]:
    """Every row a bring-up delivers by printing the steps that make it and taking a value.

    Two registers hold them and they are run the same way: the device rows,
    whose value goes into a stack's config, and the escrowed rows nothing here
    can draw, whose value goes into the registry.
    """
    return [
        *devices.DEVICES,
        *(name for name, label in escrow.rows().items() if isinstance(label.origin, escrow.Console)),
    ]


@pytest.mark.parametrize('member', console_rows())
def test_the_bring_up_order_names_every_row_made_by_hand(member: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Every one of these is delivered the same way and belongs to the same
    # stage of a bring-up, so an operator following the order and a tree built
    # from those registers have to agree about how many there are.
    order = _rendered('credentials', monkeypatch)

    assert ('derived', member, 'record') in runs_of(member, order), (
        f'`{member}` is a row a person makes but the bring-up order never runs it: '
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


def test_the_help_names_the_shared_passphrase_by_the_registers_term(monkeypatch: pytest.MonkeyPatch) -> None:
    # The passphrase every stack but `physical` and the operator stacks is
    # encrypted under is the stack passphrase, the term its register row
    # carries. The bring-up order names it where it says which Environments
    # it reaches, and the
    # row's own help names it where it says what the row holds; neither says
    # "estate", a word that means the operator's personal holdings and nothing
    # in this tree.
    def flat(name: str) -> str:
        return ' '.join(' '.join(_rendered(name, monkeypatch)).split())

    order = flat('credentials')
    row = flat(f'credentials derived {escrow.row_name(escrow.PASSPHRASE)}')

    assert 'pushes the stack passphrase into every Environment' in order
    assert 'the Pulumi stack passphrase' in row
    assert 'estate' not in order
    assert 'estate' not in row


def test_the_help_names_the_operator_passphrase_by_the_registers_term(monkeypatch: pytest.MonkeyPatch) -> None:
    # The passphrase the operator stacks are encrypted under is the operator
    # passphrase, and its row is named after it: the bring-up order runs that
    # row and says what it holds in that term, and the row's own help says the
    # same. No help text still names the row after the one stack it covered
    # first.
    def flat(name: str) -> str:
        return ' '.join(' '.join(_rendered(name, monkeypatch)).split())

    row = escrow.row_name(escrow.OPERATOR_PASSPHRASE)
    order = flat('credentials')
    own = flat(f'credentials derived {row}')

    assert ('derived', row, 'generate') in runs_of(row, _rendered('credentials', monkeypatch))
    assert 'The operator passphrase, which encrypts the operator stacks' in order
    assert 'the operator passphrase, which encrypts the operator stacks' in own
    assert 'github-passphrase' not in order
