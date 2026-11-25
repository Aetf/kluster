"""`harvest`: the places a review tags become cases of the stub gate's set, paired with the gate's run at that head.

The pull request writes three lines into a note, and one into a file the
gate draws nothing from, and the stub gate's run at
its head (`gate_stub`'s recorded replies) finds the first and passes the
other two. The review then tags the second a finding, which the gate passed,
and holds the third clean, naming it by its file name alone.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from gate_stub import (
    PULL,
    RULE,
    RULE_DOCUMENT,
    GateMemoryForge,
    commit,
    pull_request,
    recorded,
    scratch_repository,
    stub_gate,
)

from kluster.scripts.dispatch.gates import narration, run
from kluster.scripts.dispatch.gates.extract import Change, Item, message_path
from kluster.scripts.dispatch.gates.harvest import HarvestGit, harvest
from kluster.scripts.dispatch.gates.verdict import Verdict

NOTE = 'docs/notes/placement.md'
#: A file the change writes that the stub gate's extractor draws nothing from.
OTHER = 'docs/other.md'
LINES = ['The cache runs on the homelab node.', 'The cache requests a GPU.', 'The cache keeps a week of entries.']


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _reviewed(
    tmp_path: Path, repository: Path, review: str, *, mine: bool = True, ran: bool = True, status: int = 200
) -> tuple[GateMemoryForge, str]:
    """A pull request the stub gate has run on, with `review`, its `{head}` filled, on it; and its head."""
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {NOTE: '\n'.join(LINES) + '\n', OTHER: 'The queue runs on the homelab node.\n'})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    replies = recorded()
    replies.status = status
    if ran:
        _ = run.review(forge, HarvestGit(repository), PULL, [stub_gate(tmp_path / 'run')], replies.model)
    _ = forge.say(PULL, review.format(head=head[:12]), mine=mine)
    return forge, head


REVIEW = """## Review (combined)

Read at {head}.

1. **F1** `docs/notes/placement.md:2` states a placement the scheduler derives. [gate:stub]
2. `placement.md:3` states a retention the program declares, which the rule allows. [gate:stub clean]

Verdict: one finding.
"""


def test_a_reviews_tags_become_cases_and_each_disagreement_with_the_gate_is_reported(
    tmp_path: Path, repository: Path
) -> None:
    forge, head = _reviewed(tmp_path, repository, REVIEW)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert [(c.line, c.verdict, c.text, c.head) for c in stub.cases()] == [
        (2, Verdict.FINDING, LINES[1], head),
        (3, Verdict.PASS, LINES[2], head),
    ]
    assert harvested.appended == {'stub': stub.cases()}
    (url,) = {c.source for c in stub.cases()}
    assert harvested.notes == [
        f'{url}: stub disagrees with the review at {NOTE}:2: the gate said pass, the review finding',
        f'{url}: stub found {NOTE}:1 and the review tags it neither way; '
        '[gate:stub] or [gate:stub clean] there makes it a case',
    ]


def test_harvesting_the_same_review_again_appends_nothing(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW)
    stub = stub_gate(tmp_path)
    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])

    again = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert again.appended == {'stub': []}
    assert len(stub.cases()) == 2


def test_a_review_another_account_wrote_is_data_and_harvests_nothing(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW, mine=False)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert stub.cases() == []
    assert harvested.notes == [f'#{PULL} has no review this account wrote']


def test_a_place_the_extractor_draws_no_item_at_is_reported_and_not_appended(tmp_path: Path, repository: Path) -> None:
    review = '## Review (combined)\n\nAt {head}: `docs/other.md:1` reads as a placement. [gate:stub]\n'
    forge, _ = _reviewed(tmp_path, repository, review)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert stub.cases() == []
    assert any('stub draws no item at docs/other.md:1, so the gate never saw it' in note for note in harvested.notes)


def test_a_tag_on_a_file_the_change_never_touched_is_read_against_the_tree(tmp_path: Path, repository: Path) -> None:
    # `docs/rule.md` is in the tree at the head, and the change does not touch it.
    review = '## Review (combined)\n\nAt {head}: `docs/rule.md:5` no longer holds. [gate:stub]\n'
    forge, _ = _reviewed(tmp_path, repository, review)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert any('stub draws no item at docs/rule.md:5' in note for note in harvested.notes)
    assert not any('docs/rule.md is no path' in note for note in harvested.notes)


def test_an_extractor_that_raises_at_the_reviewed_head_is_reported_and_appends_nothing(
    tmp_path: Path, repository: Path
) -> None:
    def broken(change: Change) -> list[Item]:
        raise SyntaxError('invalid syntax')

    forge, _ = _reviewed(tmp_path, repository, REVIEW)
    stub = stub_gate(tmp_path)
    failing = replace(stub, spec=replace(stub.spec, extract=broken))

    harvested = harvest(forge, HarvestGit(repository), [PULL], [failing])

    assert stub.cases() == []
    assert any('stub drew nothing at' in note and 'SyntaxError: invalid syntax' in note for note in harvested.notes)


def test_a_tag_in_an_indented_paragraph_binds_and_one_naming_no_place_is_reported(
    tmp_path: Path, repository: Path
) -> None:
    review = """## Review (combined), head {head}

1. `docs/notes/placement.md:1` states where the cache runs.

   The scheduler derives that from the volume. [gate:stub]

**2. The notes hold declarations.** [gate:stub clean]
"""
    forge, _ = _reviewed(tmp_path, repository, review, ran=False)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert [(c.line, c.verdict) for c in stub.cases()] == [(1, Verdict.FINDING)]
    (url,) = {c.source for c in stub.cases()}
    assert harvested.notes == [
        f'{url}: no findings comment of {forge.requests[PULL].head[:12]} to pair the review against',
        f'{url}: a [gate:stub] tag sits in a paragraph that names no path:line; '
        'put it in the paragraph that names the place',
    ]


def test_a_tag_on_an_item_the_gate_left_undecided_is_reported_as_no_verdict(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW, status=529)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    (url,) = {c.source for c in stub.cases()}
    assert f'{url}: stub gave no verdict at {NOTE}:2, which the review tags finding' in harvested.notes
    assert not any('disagrees' in note for note in harvested.notes)


def test_a_tag_on_a_place_in_a_commits_message_binds_to_the_commit_it_names(tmp_path: Path, repository: Path) -> None:
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {OTHER: 'A note.\n'}, 'docs: a note\n\nIt was rewritten after the review.\n')
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    _ = forge.say(PULL, f'## Review\n\nRead at {head[:12]}.\n\n1. `:commit/{head[:12]}:3` narrates. [gate:stub]\n')
    stub = stub_gate(tmp_path)
    reading = replace(stub, spec=replace(stub.spec, extract=narration.extract))

    _ = harvest(forge, HarvestGit(repository), [PULL], [reading])

    assert [(c.path, c.line, c.verdict, c.text) for c in stub.cases()] == [
        (message_path(head), 3, Verdict.FINDING, 'It was rewritten after the review.')
    ]
