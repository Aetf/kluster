"""The register's table and the command tree are meant to be the same shape.

Checking it is what makes "a row with no command" a failing test rather than
a thing someone notices a year later (credentials.md §2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kluster.scripts.credentials import entries, seeds
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


def test_the_derivation_seed_entry_has_one_definition() -> None:
    assert seeds.SEED_ENTRY == entries.SEEDS['derivation'].entry


def test_console_only_seeds_are_the_manual_surface() -> None:
    # §2's two "No" rows plus the two Apps: everything a rotation must stop
    # for. The derivation seed is generated, not minted, so it is not one.
    assert set(entries.MANUAL) == {'github-dispatch', 'github-trigger', 'zerotier'}


def test_every_member_is_reachable_as_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _ = main(['seed', '--help'])

    printed = capsys.readouterr().out
    for member in entries.SEEDS:
        assert member in printed


@pytest.mark.parametrize('member', ['oci', 'cloudflare', 'zerotier'])
def test_a_registered_but_unwritten_action_says_so(
    member: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The gap is the point: these rows exist in §2, so they exist in the tree
    # and fail loudly rather than being absent.
    kit = KdbxStore.create(tmp_path / 'kit.kdbx', 'pw')

    argv = ['--kdbx', str(kit.path), 'seed', member, 'create']
    if member not in entries.MANUAL:
        # Minting from an account root needs the estate; the paths are never
        # opened, because the action refuses before it would reach them.
        argv += ['--master-entry', 'unused', '--master-kdbx', str(kit.path)]

    assert main(argv) == 1
    assert 'not yet implemented' in caplog.text


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
    for verb in ('bootstrap', 'rotate', 'derive env'):
        assert verb in printed
