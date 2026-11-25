"""`gate: falsified-claim` over real changes: the sweep its extractor runs, harvest beside it, and the gate in shadow.

The changes are excerpts of Aetf/kluster#909 (`tests/data/gates/falsified_claim/`),
which made `docs/operations.md` §3.5 carry the legacy sealing key again at
every rebuild, and left "carried by hand once" standing in two files it
never touched (its review, finding 3): `legacy-key` is the operations.md
half and those two passages, `carry` the paragraph of it that brings the
carry back, alone, and `drill-row` the one row the same pull request
rewrote in `bring-up.md`. Each is laid beside the same excerpts of
other documents of that head (`background/`), so a term weighs in the tree
what it weighs in a tree of this repository's prose, many files holding the
language's words and few its nouns.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from gate_stub import (
    EXCERPT,
    PULL,
    GateMemoryForge,
    commit,
    files,
    pull_request,
    real_change,
    recorded,
    scratch_repository,
)

from kluster.lib import workstation
from kluster.scripts.dispatch.gates import run
from kluster.scripts.dispatch.gates.calibration import Status
from kluster.scripts.dispatch.gates.cases import load
from kluster.scripts.dispatch.gates.extract import Item, change
from kluster.scripts.dispatch.gates.falsified_claim import GATE
from kluster.scripts.dispatch.gates.falsified_claim.sweep import WINDOW
from kluster.scripts.dispatch.gates.gate import CASES, named
from kluster.scripts.dispatch.gates.harvest import HarvestGit, harvest
from kluster.scripts.dispatch.gates.verdict import Verdict
from kluster.scripts.dispatch.prcheck import Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'falsified_claim'
CANON = 'docs/style/README.md'
CREDENTIALS = 'docs/credentials.md'
MIGRATION = 'docs/cluster/migration.md'
#: The pattern #909's review swept with, as a description's `## Sweep` names it.
SWEEP = '## Sweep\n\n- `carri\\w*\\s.{0,40}once` Control: "carried by hand once"\n'


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _laid(repository: Path, name: str) -> tuple[str, str]:
    canon = (workstation.repo_root() / CANON).read_text()
    return real_change(repository, DATA / name, beside={**files(DATA / 'background'), CANON: canon})


def _drawn(repository: Path, name: str, body: str = '') -> list[Item]:
    base, head = _laid(repository, name)
    return list(GATE.extract(change(Git(repository), base, head, body)))


def _holding(items: list[Item], path: str, words: str) -> list[Item]:
    return [i for i in items if i.path == path and words in ' '.join(i.text.split())]


def test_a_change_reaches_the_claims_it_made_false_in_files_it_never_touched(repository: Path) -> None:
    items = _drawn(repository, 'legacy-key')

    (row,) = _holding(items, CREDENTIALS, 'carried by hand once')
    (bullet,) = _holding(items, MIGRATION, 'carried by hand once')
    for reached in (row, bullet):
        assert 'The change at docs/operations.md' in reached.context


def test_a_pattern_the_description_sweeps_with_reaches_what_it_matches(repository: Path) -> None:
    items = _drawn(repository, 'legacy-key', SWEEP)

    for path in (CREDENTIALS, MIGRATION):
        (reached,) = _holding(items, path, 'carried by hand once')
        assert "the description's `carri\\w*\\s.{0,40}once`" in reached.context


def test_a_hunks_own_sentences_are_no_item_however_closely_they_match_it(repository: Path) -> None:
    rewritten = (DATA / 'drill-row' / 'head' / 'docs' / 'physical' / f'bring-up.md{EXCERPT}').read_text().split('\n')
    items = _drawn(repository, 'drill-row')

    # The row is line 5 of the excerpt, and the change's only hunk: every term
    # of its sentences stands in it, and the sweep still reaches elsewhere.
    assert 'The rebuild drill' in rewritten[4]
    assert items
    assert not [i for i in items if i.path == 'docs/physical/bring-up.md' and i.overlaps(5, 5)]


def _carry(repository: Path, *, reverse: bool, beside: dict[str, str] | None = None) -> list[Item]:
    """What the sweep draws from #909's carry paragraph alone, inserted or, with `reverse`, deleted."""
    canon = (workstation.repo_root() / CANON).read_text()
    laid = {**files(DATA / 'background'), CANON: canon, **(beside or {})}
    base, head = real_change(repository, DATA / 'carry', beside=laid, reverse=reverse)
    return list(GATE.extract(change(Git(repository), base, head, '')))


@pytest.mark.parametrize(('reverse', 'side'), [(False, 'Before:\n(nothing)'), (True, 'After:\n(nothing)')])
def test_a_hunk_that_only_inserts_or_only_deletes_reaches_with_the_side_it_has(
    repository: Path, reverse: bool, side: str
) -> None:
    items = _carry(repository, reverse=reverse)

    for path in (CREDENTIALS, MIGRATION):
        (reached,) = _holding(items, path, 'carried by hand once')
        assert side in reached.context


#: Two terms of the carry paragraph's sentences, "legacy" and "sealing", and the language's words between them.
NEAR = "The legacy cluster's sealing key moves with it."
APART = (
    'The legacy cluster is the one that every run of it reads from the start, and what each of them does with '
    'whatever it has when it runs again from the top of the file it is in, and then its sealing key moves.'
)
#: Two words of the carry paragraph's first sentence that the language uses everywhere.
COMMON = 'The rotation waits until the next window opens.'


def test_a_pair_farther_apart_than_the_window_is_no_item(repository: Path) -> None:
    items = _carry(repository, reverse=False, beside={'docs/near.md': NEAR, 'docs/apart.md': APART})

    assert APART.index('sealing') - APART.index('legacy') > WINDOW
    assert [i.path for i in items if i.path in {'docs/near.md', 'docs/apart.md'}] == ['docs/near.md']


def test_two_of_the_languages_common_words_close_together_reach_nothing(repository: Path) -> None:
    items = _carry(repository, reverse=False, beside={'docs/common.md': COMMON})

    assert not [i for i in items if i.path == 'docs/common.md']


def test_text_this_repository_holds_but_did_not_write_is_no_item(repository: Path) -> None:
    claim = '// The legacy sealing key is carried by hand once, at the hand-over.\n'
    items = _carry(repository, reverse=False, beside={'tests/vendor/compiler.js': claim, 'src/compiler.js': claim})

    assert [i.path for i in items if i.path.endswith('compiler.js')] == ['src/compiler.js']


@pytest.mark.parametrize(('written', 'reaches'), [('docs/carry.md', True), ('tests/vendor/carry.md', False)])
def test_a_claim_is_taken_only_from_a_file_this_repository_writes(
    repository: Path, written: str, reaches: bool
) -> None:
    # The carry paragraph of #909, lines 77–87 of the excerpt, written into a file of its own and nowhere else.
    paragraph = '\n'.join((DATA / 'carry' / 'head' / 'docs' / f'operations.md{EXCERPT}').read_text().split('\n')[76:87])
    canon = (workstation.repo_root() / CANON).read_text()
    base = commit(repository, {**files(DATA / 'background'), **files(DATA / 'carry' / 'tree'), CANON: canon})
    head = commit(repository, {written: f'{paragraph}\n'})

    items = list(GATE.extract(change(Git(repository), base, head, '')))

    reached = [i for i in items if f'The change at {written}' in i.context]
    assert 'carry of the legacy sealing key' in ' '.join(paragraph.split())
    assert bool(reached) is reaches
    assert bool(_holding(reached, CREDENTIALS, 'carried by hand once')) is reaches


def test_a_review_tag_on_a_file_the_change_never_touched_becomes_a_case(tmp_path: Path, repository: Path) -> None:
    base, head = _laid(repository, 'legacy-key')
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    gate = named('falsified-claim')
    copy = tmp_path / 'falsified_claim'
    copy.mkdir()
    moved = replace(gate, directory=copy)
    (row,) = _holding(list(GATE.extract(change(Git(repository), base, head, ''))), CREDENTIALS, 'carried by hand once')
    _ = forge.say(
        PULL,
        f'## Review (combined)\n\nRead at {head[:12]}.\n\n'
        f'**3.** `{CREDENTIALS}:{row.line}` still says "carried by hand once". [gate:falsified-claim]\n',
    )

    harvested = harvest(forge, HarvestGit(repository), [PULL], [moved])

    (case,) = load(copy / CASES)
    assert (case.path, case.line, case.verdict) == (CREDENTIALS, row.line, Verdict.FINDING)
    assert harvested.appended['falsified-claim'] == [case]


def test_the_gate_runs_in_shadow_end_to_end_against_a_recorded_reply(repository: Path) -> None:
    base, head = _laid(repository, 'drill-row')
    forge = GateMemoryForge()
    pull_request(forge, base, head)

    posted = run.review(forge, Git(repository), PULL, [named('falsified-claim')], recorded(DATA / 'messages').model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert (ran.gate, ran.status, ran.asked) == ('falsified-claim', Status.SHADOW, True)
    assert [e.place for e in ran.entries] == [i.place for i in GATE.extract(change(Git(repository), base, head, ''))]
    assert ran.findings
    assert not run.refuses(0, posted)
