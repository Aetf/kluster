"""The seed register's own rules, and the commands that read it without a kit.

What a §2 row must be: an entry one group deep, an identifier that is never
blank, no kit row the escrow also holds, and the Cloudflare console text asking
for both of the seed's permissions. Which kind of row a member is -- minted,
console-made, or the recovery key that alone mints nothing -- is its
declaration's to say (`entries`), and the kinds' fields are what enforce it;
what is held here is what each kind makes of a row. Beside them, the root
listing that needs no kit, the console-only `create` that stops with its
console steps, and the help's ordering of the lifecycle. That every register
row has a command is the walk over the real parser, `tests/test_cli.py`'s
`test_the_walk_finds_every_register_row`.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest
from credentials_command_tree import commands

from kluster.scripts.credentials import cloudflare, entries, escrow, masters
from kluster.scripts.credentials.cli import main
from kluster.scripts.credentials.kdbx import KdbxStore


def test_every_seed_lives_one_group_deep() -> None:
    for seed in entries.SEEDS.values():
        assert seed.entry.count('/') == 1


def test_no_seed_leaves_its_identifier_empty() -> None:
    # UserName always holds the public half, so the field is never a secret
    # and never blank.
    for seed in entries.SEEDS.values():
        assert seed.identifier


def test_a_console_seed_is_the_manual_kind_and_a_minted_one_is_not() -> None:
    # What a rotation stops for: a console seed is manual by its steps. A
    # minted seed has no steps to carry, whatever it can mint.
    console = entries.ConsoleSeed(
        member='example', title='Example', identifier='its id', mints='a credential', console='a console → a page.'
    )
    minted = entries.MintedSeed(
        member='example', title='Example', identifier='its id', mints='a credential', mints_own_successor=True
    )

    assert console.manual
    assert not minted.manual


def test_the_recovery_kind_alone_is_declared_without_stating_what_it_mints() -> None:
    # The one kind exempt from minting (§2): it is declared with its name and
    # its identifier alone, and still has words for the listing that says what
    # each seed mints.
    recovery = entries.RecoverySeed(member='example', title='Example', identifier='its recipient')

    assert recovery.mints.strip()
    assert not recovery.manual


def test_nothing_the_escrow_holds_is_also_a_kit_row() -> None:
    # The membership rule from its expensive side. A credential with two homes
    # makes the kit's own rotation owe a redelivery to whatever reads the other
    # copy, which is precisely what stops a kit from being rotatable on its own
    # schedule. The rows that carry a kit address are exactly the ones a kit
    # may still hold, so declaring one in this table again is what this
    # catches.
    held = {seed.entry for seed in entries.SEEDS.values()}
    addressed = {
        row.origin.kit.entry
        for row in escrow.register().values()
        if isinstance(row.origin, escrow.Console) and row.origin.kit is not None
    }

    assert addressed
    assert not addressed & held


def test_the_cloudflare_console_text_asks_for_both_of_the_seed_s_permissions() -> None:
    console = entries.SEEDS['cloudflare'].console

    # The console text is the only instruction the operator gets, and the seed
    # is used for two things: minting tokens and resolving zone names to the
    # ids a minted policy carries. A text naming only the first mints a seed
    # that adoption refuses.
    assert 'API Tokens → Edit' in console
    assert cloudflare.ZONE_VISIBILITY_PERMISSION in console


def test_listing_the_roots_needs_no_kit_and_prints_no_value(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv('KLUSTER_KDBX', raising=False)

    def held(_account: str) -> str | None:
        return 'a-secret'

    monkeypatch.setattr('kluster.scripts.credentials.kdbx.remembered', held)

    assert main(['root', 'ls']) == 0

    printed = capsys.readouterr().out
    assert 'a-secret' not in printed
    for member in masters.ROOTS:
        assert member in printed


def test_a_row_no_api_can_create_stops_rather_than_inventing_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No API can create the Cloudflare seed, so the tree still carries the
    # command and the command still stops -- with the console steps, not with a
    # stack trace.
    kit = KdbxStore.create(tmp_path / 'kit.kdbx', 'pw')
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': '')

    assert main(['--kdbx', str(kit.path), 'seed', 'cloudflare', 'create']) == 1
    for line in entries.SEEDS['cloudflare'].console.splitlines():
        assert line.strip() in caplog.text


def test_the_help_says_when_to_run_what(capsys: pytest.CaptureFixture[str]) -> None:
    """The tree says what exists; the epilog says what to do with it.

    A register-shaped command list answers neither "where do I start" nor
    "which of these destroys something", so the ordering is part of the help
    rather than a document someone has to know exists.
    """
    with pytest.raises(SystemExit):
        _ = main(['--help'])

    printed = capsys.readouterr().out
    # Each command the ordering spells is a leaf the tree still carries, up
    # to the first placeholder or option, so a verb renamed in the parser is
    # red here while the hand-written ordering still names the old one. A
    # command stands on a line of its own, step number aside: a line of prose
    # that a wrap starts with the word reads on into punctuation.
    spelled = [
        list(itertools.takewhile(lambda word: '<' not in word and not word.startswith('-'), found.group(1).split()))
        for line in printed.splitlines()
        if (found := re.fullmatch(r'\s*(?:\d+\.\s+)?credentials((?: [a-z0-9<>-]+)+)\s*', line))
    ]
    leaves = commands()
    assert spelled
    for words in spelled:
        assert words in [leaf[: len(words)] for leaf in leaves], words
