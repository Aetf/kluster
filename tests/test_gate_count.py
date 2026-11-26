"""`gate: count` over real changes: what its extractor draws.

Each change is excerpts of a pull request a review found counts in
(`tests/data/gates/count/`): Aetf/kluster#927's tripwire bound, which counts
images across a soft-wrapped comment; Aetf/kluster#890's watchdog module,
whose counts sit in a module docstring, a comment and an attribute's
docstring; Aetf/kluster#899's block I/O class in `nodes.md`, a rewrite
beside lines it leaves alone; and `table`, a stretch of
`src/kluster/lib/state_backend/table.py` where an `f` string precedes a
function and its docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gate_stub import EXCERPT, files, real_change, scratch_repository

from kluster.scripts.dispatch.gates.count import GATE
from kluster.scripts.dispatch.gates.extract import Item, change
from kluster.scripts.dispatch.gates.prose import blocks
from kluster.scripts.dispatch.prcheck import Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'count'


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _drawn(repository: Path, name: str, fixture: Path | None = None) -> list[Item]:
    base, head = real_change(repository, fixture or DATA / name)
    return list(GATE.extract(change(Git(repository), base, head, '')))


def _at(items: list[Item], path: str) -> dict[int, Item]:
    return {i.line: i for i in items if i.path == path}


def test_a_count_soft_wrapped_across_two_comment_lines_is_one_item(repository: Path) -> None:
    items = _at(_drawn(repository, 'operator-updates'), 'tests/test_operator_updates.py')

    # "the pulls of four\n#: images": the wrap falls between the number and what it counts.
    wrapped = items[5]
    assert (wrapped.line, wrapped.end_line) == (5, 6)
    assert 'four' in wrapped.text
    assert 'images' in wrapped.text


def test_a_python_files_comments_and_docstrings_are_each_read_with_what_follows(repository: Path) -> None:
    items = _at(_drawn(repository, 'watchdog'), 'src/kluster/components/apps/immich/watchdog.py')

    # The module docstring's "two parts:", whose members are the list below it.
    assert 'two parts' in items[11].text
    assert '`SERVER_ENV`' in items[11].context
    assert '`job_slot_watchdog`' in items[11].context
    # A paragraph further down the same docstring, an attribute's docstring and a `#:` comment.
    assert "family's two rules" in items[31].text
    assert "family's two rules" in items[131].text
    assert 'Every ten minutes' in items[84].text


def test_only_counts_on_lines_the_change_writes_are_drawn_each_as_its_own_item(repository: Path) -> None:
    head = (DATA / 'nodes' / 'head' / 'docs' / 'cluster' / f'nodes.md{EXCERPT}').read_text().split('\n')
    items = _at(_drawn(repository, 'nodes'), 'docs/cluster/nodes.md')

    # Line 4 is outside the hunk, and counts "four operations" all the same.
    assert 'four operations' in head[3]
    assert 4 not in items
    # The count the review kept and the one it struck sit on adjacent lines, and are two places.
    assert 'which 15 containers' in items[37].text
    assert 'about 13 of the containers' in items[38].text


def test_python_prose_is_its_docstrings_and_comments_and_no_line_of_code_or_of_a_string_value() -> None:
    # Lines 395–454 of `src/kluster/lib/state_backend/table.py`: an `f` string, then a function.
    text = (DATA / 'table' / f'table.py{EXCERPT}').read_text()
    lines = text.split('\n')
    read = {block.first: block for block in blocks('src/kluster/lib/state_backend/table.py', text)}

    prune = lines.index('def prune(target: Target, keys: Sequence[str], *, database: str) -> list[str]:') + 1
    assert read[prune + 1].text.startswith('Delete the superseded copies named')
    assert read[1].text.startswith('whose stack has a checkpoint')
    held = {n for block in read.values() for n, _ in block.pieces}
    assert not held & {lines.index('WITH asked AS (') + 1, prune}


def test_a_count_in_text_this_repository_holds_but_did_not_write_is_no_item(tmp_path: Path, repository: Path) -> None:
    vendored = tmp_path / 'vendored'
    for path, text in files(DATA / 'operator-updates' / 'head').items():
        target = vendored / 'head' / 'tests' / 'vendor' / f'{path}{EXCERPT}'
        target.parent.mkdir(parents=True)
        _ = target.write_text(text)

    assert _drawn(repository, 'operator-updates')
    assert _drawn(repository, '', fixture=vendored) == []
