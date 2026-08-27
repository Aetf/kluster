"""The register's table and the command tree are meant to be the same shape.

Checking it is what makes "a row with no command" a failing test rather than
a thing someone notices a year later (credentials.md §2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kluster.scripts.credentials import cloudflare, entries, escrow, masters
from kluster.scripts.credentials.cli import main
from kluster.scripts.credentials.kdbx import KdbxStore


def test_every_seed_lives_one_group_deep() -> None:
    for seed in entries.SEEDS.values():
        assert seed.entry == f'{entries.GROUP}/{seed.title}'
        assert seed.entry.count('/') == 1


def test_no_seed_leaves_its_identifier_empty() -> None:
    # UserName always holds the public half, so the field is never a secret
    # and never blank.
    for seed in entries.SEEDS.values():
        assert seed.identifier


def test_the_recovery_key_entry_has_one_definition() -> None:
    assert escrow.RECOVERY_ENTRY == entries.SEEDS['recovery'].entry


def test_console_only_seeds_are_the_manual_surface() -> None:
    # Everything a rotation must stop for: the two Apps, whose key generation
    # is console-only, and Cloudflare, which forbids a minted token from
    # carrying token permissions and so has no credential able to mint the
    # seed. The recovery key is generated rather than minted, so it is not one
    # of them.
    assert set(entries.MANUAL) == {'cloudflare', 'github-dispatch', 'github-trigger'}


def test_only_the_recovery_key_mints_nothing() -> None:
    # §2's membership rule as a check rather than as prose: a kit row earns its
    # place by minting successors, and the one exception is the recovery key,
    # which opens what the escrow holds. A credential that does neither is a §3
    # row delivered into its consumer's slot, however console-bound its
    # creation is -- without that line the kit becomes a token drawer again.
    barren = {member for member, seed in entries.SEEDS.items() if seed.mints.startswith('nothing')}

    assert barren == {'recovery'}


def test_the_cloudflare_console_text_asks_for_both_of_the_seed_s_permissions() -> None:
    console = entries.SEEDS['cloudflare'].console

    # The console text is the only instruction the operator gets, and the seed
    # is used for two things: minting tokens and resolving zone names to the
    # ids a minted policy carries. A text naming only the first mints a seed
    # that adoption refuses.
    assert 'API Tokens → Edit' in console
    assert cloudflare.ZONE_VISIBILITY_PERMISSION in console


def test_every_member_is_reachable_as_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _ = main(['seed', '--help'])

    printed = capsys.readouterr().out
    for member in entries.SEEDS:
        assert member in printed


def test_every_account_root_is_reachable_as_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    # The roots are a register too (§2): one `root <name>` command each, so
    # a root the scripts borrow has a place to be put and to be listed.
    with pytest.raises(SystemExit):
        _ = main(['root', '--help'])

    printed = capsys.readouterr().out
    for member in masters.ROOTS:
        assert member in printed


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
        assert f'{member}: in the secret store' in printed


def test_a_row_no_api_can_create_stops_rather_than_inventing_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No API can create the Cloudflare seed, so the tree still carries the
    # command and the command still stops -- with the console steps, not with a
    # stack trace.
    kit = KdbxStore.create(tmp_path / 'kit.kdbx', 'pw')
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': '')

    assert main(['--kdbx', str(kit.path), 'seed', 'cloudflare', 'create']) == 1
    assert 'dash.cloudflare.com' in caplog.text


def test_the_help_says_when_to_run_what(capsys: pytest.CaptureFixture[str]) -> None:
    """The tree says what exists; the epilog says what to do with it.

    A register-shaped command list answers neither "where do I start" nor
    "which of these destroys something", so the ordering is part of the help
    rather than a document someone has to know exists.
    """
    with pytest.raises(SystemExit):
        _ = main(['--help'])

    printed = capsys.readouterr().out
    for landmark in ('bring-up, from nothing', 'when one seed is lost', 'rotation', 'day to day'):
        assert landmark in printed
    # Each lifecycle verb appears in the ordering, not only in the tree.
    for verb in ('kit bootstrap', 'kit rotate', 'derived <row> generate', 'derived check'):
        assert verb in printed
