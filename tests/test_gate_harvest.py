"""`harvest`: the places a review tags become cases of the stub gate's history, paired with the gate's run at that head.

The pull request writes three lines into a note, each far enough from the
next that no two lie within the margin, one into another file the gate is
shown, and one into a module outside the gate's trigger. The stub gate's run at its head finds the first line in two
samples of three, which stands, and the third in one alone. No sample
finds the second, which the gate was shown and so passed. The review then
tags the second a finding, and holds the third clean, naming it by its file
name alone.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from gate_stub import (
    PULL,
    RULE,
    RULE_DOCUMENT,
    SPEC,
    GateMemoryForge,
    Recorded,
    commit,
    pull_request,
    reply,
    scratch_repository,
    stub_gate,
)

from kluster.scripts.dispatch.gates import run
from kluster.scripts.dispatch.gates.calibration import MARGIN
from kluster.scripts.dispatch.gates.cases import Case
from kluster.scripts.dispatch.gates.change import message_path
from kluster.scripts.dispatch.gates.gate import CASES, Gate
from kluster.scripts.dispatch.gates.harvest import HarvestGit, Kind, Note, harvest
from kluster.scripts.dispatch.gates.model import KeyMissing, Model
from kluster.scripts.dispatch.gates.verdict import Verdict

NOTE = 'docs/notes/placement.md'
#: Another file the change writes, which the stub gate is shown too.
OTHER = 'docs/other.md'
#: A file the change writes outside the stub gate's trigger.
CODE = 'src/app.py'
#: The note's lines, at 1, 5 and 9.
LINES = ['The cache runs on the homelab node.', 'The cache requests a GPU.', 'The cache keeps a week of entries.']
TEXT = '\n\n\n\n'.join(LINES) + '\n'


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _replies(status: int = 200) -> Recorded:
    """The three samples: the first line in two, the third in one."""

    def at(line: int) -> dict[str, object]:
        return {'path': NOTE, 'line': line, 'end_line': line, 'why': f'line {line} states a placement'}

    return Recorded([reply([at(1)]), reply([at(1), at(9)]), reply([])], status=status)


def _reviewed(
    tmp_path: Path,
    repository: Path,
    review: str,
    *,
    mine: bool = True,
    ran: bool = True,
    status: int = 200,
    model: Callable[[], Model] | None = None,
) -> tuple[GateMemoryForge, str]:
    """A pull request the stub gate has run on, with `review`, its `{head}` filled, on it."""
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {NOTE: TEXT, OTHER: 'The queue runs on the homelab node.\n', CODE: 'NODE = 1\n'})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    if ran:
        _ = run.review(
            forge, HarvestGit(repository), PULL, [stub_gate(tmp_path / 'run')], model or _replies(status).model
        )
    _ = forge.say(PULL, review.format(head=head[:12]), mine=mine)
    return forge, head


REVIEW = """## Review (combined)

Read at {head}.

1. **F1** `docs/notes/placement.md:5` states a placement the scheduler derives. [gate:stub]
2. `placement.md:9` states a retention the program declares, which the rule allows. [gate:stub clean]

Verdict: one finding.
"""


def _kinds(notes: list[Note]) -> list[tuple[Kind, str]]:
    return [(note.kind, note.place) for note in notes]


def test_a_reviews_tags_become_cases_and_each_disagreement_with_the_gate_is_reported(
    tmp_path: Path, repository: Path
) -> None:
    forge, head = _reviewed(tmp_path, repository, REVIEW)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert [(c.pr, c.path, c.line, c.verdict, c.head) for c in stub.cases()] == [
        (PULL, NOTE, 5, Verdict.FINDING, head),
        (PULL, NOTE, 9, Verdict.PASS, head),
    ]
    assert harvested.appended == {'stub': stub.cases()}
    assert _kinds(harvested.notes) == [(Kind.DISAGREES, f'{NOTE}:5'), (Kind.UNTAGGED, f'{NOTE}:1')]


def _no_key() -> Model:
    raise KeyMissing('no model gates key on this machine')


@pytest.mark.parametrize('why', ['not asked', 'a version 1 record'])
def test_a_place_with_no_entry_is_no_pass_where_the_run_did_not_judge_it(
    tmp_path: Path, repository: Path, why: str
) -> None:
    if why == 'not asked':
        forge, _ = _reviewed(tmp_path, repository, REVIEW, model=_no_key)
    else:
        forge, _ = _reviewed(tmp_path, repository, REVIEW)
        held = forge.pull_comments(PULL)[0]
        forge.edit(held.id, held.body.replace('{"version":2,', '{"version":1,'))
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert [c.line for c in stub.cases()] == [5, 9]
    assert Kind.DISAGREES not in {note.kind for note in harvested.notes}


def test_harvesting_the_same_review_again_records_nothing_and_reports_no_replacement(
    tmp_path: Path, repository: Path
) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW)
    stub = stub_gate(tmp_path)
    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])

    again = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert again.appended == {'stub': []}
    assert len(stub.cases()) == 2
    assert {Kind.REPLACED, Kind.KEPT}.isdisjoint(note.kind for note in again.notes)


def test_a_review_another_account_wrote_is_data_and_harvests_nothing(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW, mine=False)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert stub.cases() == []
    assert _kinds(harvested.notes) == [(Kind.UNREAD, '')]


@pytest.mark.parametrize(
    ('place', 'path'),
    [
        # In the tree at the head, and no file the change writes.
        pytest.param('docs/rule.md:5', 'docs/rule.md', id='unchanged'),
        # A file the change writes that the gate's trigger does not match.
        pytest.param(f'{CODE}:1', CODE, id='outside-the-trigger'),
        # Past the last line of a file the gate is shown.
        pytest.param(f'{NOTE}:12', NOTE, id='past-the-end'),
    ],
)
def test_a_place_the_gate_is_not_shown_at_the_reviewed_head_makes_no_case(
    tmp_path: Path, repository: Path, place: str, path: str
) -> None:
    review = f'## Review (combined)\n\nAt {{head}}: `{place}` states a placement. [gate:stub]\n'
    forge, _ = _reviewed(tmp_path, repository, review)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert stub.cases() == []
    assert (Kind.NOT_SHOWN, path) in _kinds(harvested.notes)


def test_a_tag_outside_the_margin_of_every_line_the_change_writes_makes_no_case_and_is_reported(
    tmp_path: Path, repository: Path
) -> None:
    lines = [f'The queue holds item {n}.' for n in range(1, 13)]
    base = commit(repository, {RULE_DOCUMENT: RULE, OTHER: '\n'.join(lines) + '\n'})
    # Line 6 rewritten: lines within the margin of it make cases, and line 6 + MARGIN + 1 does not.
    head = commit(
        repository, {OTHER: '\n'.join([*lines[:5], 'The queue runs on the homelab node.', *lines[6:]]) + '\n'}
    )
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    far = 6 + MARGIN + 1
    _ = forge.say(
        PULL,
        f'## Review (combined)\n\nRead at {head[:12]}.\n\n'
        f'- `{OTHER}:6` states a placement. [gate:stub]\n'
        f'- `{OTHER}:{6 + MARGIN}` holds an item. [gate:stub clean]\n'
        f'- `{OTHER}:{far}` holds an item. [gate:stub clean]\n',
    )
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert [(c.line, c.verdict) for c in stub.cases()] == [(6, Verdict.FINDING), (6 + MARGIN, Verdict.PASS)]
    assert (Kind.UNWRITTEN, f'{OTHER}:{far}') in _kinds(harvested.notes)


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
    assert _kinds(harvested.notes) == [(Kind.UNPAIRED, ''), (Kind.NO_PLACE, '')]


def test_a_tag_on_a_place_the_gate_left_undecided_is_reported_as_no_verdict(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW, status=529)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert (Kind.UNDECIDED, f'{NOTE}:5') in _kinds(harvested.notes)
    assert Kind.DISAGREES not in {note.kind for note in harvested.notes}


def test_a_tag_on_a_place_in_a_commits_message_binds_to_the_commit_it_names(tmp_path: Path, repository: Path) -> None:
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {OTHER: 'A note.\n'}, 'docs: a note\n\nIt was rewritten after the review.\n')
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    _ = forge.say(
        PULL, f'## Review (combined)\n\nRead at {head[:12]}.\n\n1. `:commit/{head[:12]}:3` narrates. [gate:stub]\n'
    )
    stub = stub_gate(tmp_path)
    # A trigger of every path takes the commits' messages too.
    every: Gate = replace(stub, spec=replace(stub.spec, trigger=('**',)))

    _ = harvest(forge, HarvestGit(repository), [PULL], [every])

    assert [(c.path, c.line, c.verdict) for c in stub.cases()] == [(message_path(head), 3, Verdict.FINDING)]


def _ruled(forge: GateMemoryForge, number: int, body: str) -> None:
    """Pull request `number`, another than `PULL`, with `body` on it as a comment this account wrote."""
    held = forge.requests[PULL]
    forge.requests[number] = replace(held, number=number, title='gates: a calibration')
    _ = forge.say(number, body)


def test_a_ruling_on_another_pull_request_records_the_place_at_the_pull_request_and_head_it_names(
    tmp_path: Path, repository: Path
) -> None:
    forge, head = _reviewed(tmp_path, repository, REVIEW, ran=False)
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:1 at {head} [gate:stub clean]\n- `{OTHER}:1` [gate:stub]\n')
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL + 1], [stub])

    # The place it names whole binds there; the one it names by its path alone is at no head.
    assert [(c.pr, c.head, c.path, c.line, c.verdict) for c in stub.cases()] == [(PULL, head, NOTE, 1, Verdict.PASS)]
    assert _kinds(harvested.notes) == [(Kind.NO_HEAD, OTHER)]


def _verdicts(stub: Gate) -> list[tuple[int, Verdict, bool]]:
    return [(c.line, c.verdict, c.operator) for c in stub.cases()]


@pytest.mark.parametrize(
    ('first', 'second', 'held', 'noted'),
    [
        # A later ruling replaces an earlier one.
        pytest.param('[gate:stub]', '[gate:stub clean]', (Verdict.PASS, False), Kind.REPLACED, id='later'),
        # An operator's ruling replaces any other.
        pytest.param('[gate:stub]', '[gate:stub clean operator]', (Verdict.PASS, True), Kind.REPLACED, id='operator'),
        # No other replaces an operator's.
        pytest.param('[gate:stub operator]', '[gate:stub clean]', (Verdict.FINDING, True), Kind.KEPT, id='kept'),
    ],
)
def test_a_second_ruling_at_a_place_replaces_the_first_or_is_kept_out_and_either_is_reported(
    tmp_path: Path, repository: Path, first: str, second: str, held: tuple[Verdict, bool], noted: Kind
) -> None:
    review = '## Review (combined)\n\nAt {head}: `docs/notes/placement.md:5` states a placement. ' + first + '\n'
    forge, head = _reviewed(tmp_path, repository, review, ran=False)
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:5 at {head} {second}\n')
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL, PULL + 1], [stub])

    assert _verdicts(stub) == [(5, *held)]
    assert (noted, f'{NOTE}:5') in _kinds(harvested.notes)


#: Each order a review on `PULL` and a later ruling on `PULL + 1` can be harvested in, then the review again.
ORDERS = [
    pytest.param([[PULL, PULL + 1], [PULL]], id='review-ruling-review'),
    pytest.param([[PULL + 1, PULL], [PULL]], id='ruling-review-review'),
    pytest.param([[PULL + 1], [PULL], [PULL, PULL + 1]], id='apart'),
]


@pytest.mark.parametrize('orders', ORDERS)
def test_the_ruling_written_later_stands_whatever_order_harvest_reads_them_in_and_again_writes_nothing(
    tmp_path: Path, repository: Path, orders: list[list[int]]
) -> None:
    review = '## Review (combined)\n\nAt {head}: `docs/notes/placement.md:5` states a placement. [gate:stub]\n'
    forge, head = _reviewed(tmp_path, repository, review, ran=False)
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:5 at {head} [gate:stub clean]\n')
    stub = stub_gate(tmp_path)
    *first, last = orders
    for numbers in first:
        _ = harvest(forge, HarvestGit(repository), numbers, [stub])
    held = (stub.directory / CASES).read_text()

    again = harvest(forge, HarvestGit(repository), last, [stub])

    assert _verdicts(stub) == [(5, Verdict.PASS, False)]
    assert (stub.directory / CASES).read_text() == held
    assert {Kind.REPLACED, Kind.KEPT}.isdisjoint(note.kind for note in again.notes)


@pytest.mark.parametrize('numbers', [[PULL, PULL + 1], [PULL + 1, PULL]], ids=['in-order', 'reversed'])
def test_of_two_operator_rulings_at_a_place_the_later_stands(
    tmp_path: Path, repository: Path, numbers: list[int]
) -> None:
    review = '## Review (combined)\n\nAt {head}: `docs/notes/placement.md:5` places it. [gate:stub operator]\n'
    forge, head = _reviewed(tmp_path, repository, review, ran=False)
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:5 at {head} [gate:stub clean operator]\n')
    stub = stub_gate(tmp_path)

    _ = harvest(forge, HarvestGit(repository), numbers, [stub])

    assert _verdicts(stub) == [(5, Verdict.PASS, True)]


@pytest.mark.parametrize(
    'tag', ['[gate:stub operator clean]', '[Gate:stub]', '[gate:stub  clean]', '[gate:stub, operator]']
)
def test_text_that_opens_like_a_tag_and_does_not_read_as_one_is_reported(
    tmp_path: Path, repository: Path, tag: str
) -> None:
    forge, head = _reviewed(tmp_path, repository, '## Review (combined)\n\nAt {head}.\n', ran=False)
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:5 at {head} {tag}\n')
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL + 1], [stub])

    assert stub.cases() == []
    assert [n.text.removesuffix(' opens like a tag and does not read as one') for n in harvested.notes] == [tag]


@pytest.mark.parametrize(
    'ruled',
    [
        pytest.param(
            '- `docs/notes/placement.md:1` [gate:stub] and `docs/notes/placement.md:9` [gate:stub clean]\n',
            id='one-item',
        ),
        pytest.param(
            '- `docs/notes/placement.md:1` [gate:stub]\n- `docs/notes/placement.md:1` [gate:stub clean]\n',
            id='one-place-two-ways',
        ),
    ],
)
def test_one_gates_rulings_that_disagree_at_one_unit_or_one_place_bind_none_of_them(
    tmp_path: Path, repository: Path, ruled: str
) -> None:
    review = '## Review (combined)\n\nAt {head}.\n\n' + ruled + '- `docs/notes/placement.md:5` [gate:stub]\n'
    forge, _ = _reviewed(tmp_path, repository, review, ran=False)
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert _verdicts(stub) == [(5, Verdict.FINDING, False)]
    assert Kind.AMBIGUOUS in {note.kind for note in harvested.notes}


def test_each_line_of_a_paragraph_binds_its_own_places(tmp_path: Path, repository: Path) -> None:
    review = (
        '## Review (combined)\n\nAt {head}.\n\n'
        '`docs/notes/placement.md:1` states a placement. [gate:stub]\n'
        '`docs/notes/placement.md:9` states a retention. [gate:stub clean]\n'
    )
    forge, _ = _reviewed(tmp_path, repository, review, ran=False)
    stub = stub_gate(tmp_path)

    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert _verdicts(stub) == [(1, Verdict.FINDING, False), (9, Verdict.PASS, False)]


def test_a_place_named_at_a_head_that_is_no_commit_of_its_pull_request_makes_no_case(
    tmp_path: Path, repository: Path
) -> None:
    forge, _ = _reviewed(tmp_path, repository, '## Review (combined)\n\nAt {head}.\n', ran=False)
    # The pull request's base: a commit the store holds, of no pull request here.
    elsewhere = forge.requests[PULL].base
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {RULE_DOCUMENT}:5 at {elsewhere} [gate:stub]\n')
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL + 1], [stub])

    assert stub.cases() == []
    assert _kinds(harvested.notes) == [(Kind.FOREIGN, RULE_DOCUMENT)]


def test_a_head_the_gates_history_holds_a_case_of_its_pull_request_at_is_one_of_its_commits(
    tmp_path: Path, repository: Path
) -> None:
    forge, _ = _reviewed(tmp_path, repository, '## Review (combined)\n\nAt {head}.\n', ran=False)
    # A head the pull request no longer holds and no comment names, as a head pushed over is.
    earlier = commit(repository, {NOTE: TEXT.replace('GPU', 'disk')})
    stub = stub_gate(tmp_path)
    held = Case(PULL, earlier, NOTE, 1, 1, Verdict.FINDING, 'https://example.invalid/review')
    _ = (stub.directory / CASES).write_text(f'{held.to_json()}\n')
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:9 at {earlier} [gate:stub clean]\n')

    _ = harvest(forge, HarvestGit(repository), [PULL + 1], [stub])

    assert [(c.head, c.line, c.verdict) for c in stub.cases()] == [
        (earlier, 1, Verdict.FINDING),
        (earlier, 9, Verdict.PASS),
    ]


def test_a_reviews_own_head_is_never_one_it_names_a_place_at(tmp_path: Path, repository: Path) -> None:
    forge, head = _reviewed(tmp_path, repository, '## Review (combined)\n\nAt {head}.\n', ran=False)
    other = commit(repository, {NOTE: TEXT.replace('GPU', 'disk')})
    forge.requests[PULL + 1] = replace(forge.requests[PULL], number=PULL + 1, head=other)
    review = (
        f'## Review (combined)\n\n- #{PULL + 1} {NOTE}:5 at {other} [gate:stub clean]\n\n'
        f'Read at {head[:12]}.\n\n- `{NOTE}:1` states a placement. [gate:stub]\n'
    )
    _ = forge.say(PULL, review)
    stub = stub_gate(tmp_path)

    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert sorted((c.pr, c.head, c.line, c.verdict) for c in stub.cases()) == [
        (PULL, head, 1, Verdict.FINDING),
        (PULL + 1, other, 5, Verdict.PASS),
    ]


def test_a_comment_headed_otherwise_than_a_review_or_a_ruling_is_not_read(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, '## Review answers\n\nAt {head}: `placement.md:5` [gate:stub]\n')
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert stub.cases() == []
    assert _kinds(harvested.notes) == [(Kind.UNREAD, '')]


def _other_gate(tmp_path: Path) -> Gate:
    """The stub gate under another name, `other`, its history its own."""
    copy = tmp_path / 'other'
    _ = shutil.copytree(stub_gate(tmp_path / 'source').directory, copy)
    return Gate.load(copy, SPEC)


@pytest.mark.parametrize(
    ('line', 'bound'),
    [
        # Each gate's tag beside its own place, on one line: nothing says which is which.
        pytest.param(
            f'- `{NOTE}:1` states a placement [gate:stub]; `{OTHER}:1` counts [gate:other]', [], id='two-places'
        ),
        # One place takes every gate's tag beside it.
        pytest.param(f'- `{NOTE}:1` states a placement. [gate:stub] [gate:other]', [(NOTE, 1)], id='one-place'),
    ],
)
def test_two_gates_tags_bind_in_a_unit_only_where_it_names_one_place(
    tmp_path: Path, repository: Path, line: str, bound: list[tuple[str, int]]
) -> None:
    forge, _ = _reviewed(tmp_path, repository, '## Review (combined)\n\nAt {head}.\n\n' + line + '\n', ran=False)
    stub, other = stub_gate(tmp_path), _other_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub, other])

    assert [[(c.path, c.line) for c in g.cases()] for g in (stub, other)] == [bound, bound]
    assert (Kind.AMBIGUOUS in {note.kind for note in harvested.notes}) is not bool(bound)


def test_a_nested_list_items_tag_binds_its_own_places_alone(tmp_path: Path, repository: Path) -> None:
    review = (
        '## Review (combined)\n\nAt {head}.\n\n'
        '1. **nit**: two places the sweep found, `docs/notes/placement.md:5` among them:\n'
        '   - `docs/notes/placement.md:1` states a placement. [gate:stub]\n'
        '   - `docs/notes/placement.md:9` holds the rule as it stands.\n'
    )
    forge, _ = _reviewed(tmp_path, repository, review, ran=False)
    stub = stub_gate(tmp_path)

    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert _verdicts(stub) == [(1, Verdict.FINDING, False)]


def test_a_review_headed_review_and_a_colon_is_read(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(
        tmp_path, repository, '## Review: style angle, head {head}\n\n- `placement.md:5` [gate:stub]\n'
    )
    stub = stub_gate(tmp_path)

    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert _verdicts(stub) == [(5, Verdict.FINDING, False)]


def test_an_edited_comments_new_verdict_replaces_what_it_recorded(tmp_path: Path, repository: Path) -> None:
    forge, _ = _reviewed(tmp_path, repository, REVIEW, ran=False)
    stub = stub_gate(tmp_path)
    _ = harvest(forge, HarvestGit(repository), [PULL], [stub])
    review = forge.pull_comments(PULL)[-1]
    forge.edit(review.id, review.body.replace('derives. [gate:stub]', 'derives. [gate:stub clean]'))

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert _verdicts(stub) == [(5, Verdict.PASS, False), (9, Verdict.PASS, False)]
    assert (Kind.REPLACED, f'{NOTE}:5') in _kinds(harvested.notes)


def test_a_ruling_written_before_the_operators_at_its_place_is_not_reported_as_kept_out(
    tmp_path: Path, repository: Path
) -> None:
    review = '## Review (combined)\n\nAt {head}: `docs/notes/placement.md:5` states a placement. [gate:stub]\n'
    forge, head = _reviewed(tmp_path, repository, review, ran=False)
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:5 at {head} [gate:stub clean operator]\n')
    stub = stub_gate(tmp_path)
    _ = harvest(forge, HarvestGit(repository), [PULL, PULL + 1], [stub])

    again = harvest(forge, HarvestGit(repository), [PULL], [stub])

    assert _verdicts(stub) == [(5, Verdict.PASS, True)]
    assert Kind.KEPT not in {note.kind for note in again.notes}


@pytest.mark.parametrize(
    ('read', 'foreign'),
    [
        # The head a review of the pull request read, which the pull request no longer holds.
        pytest.param(True, False, id='a-head-a-review-read'),
        # A commit a review cites in its prose, past the head it read.
        pytest.param(False, True, id='a-commit-a-review-cites'),
    ],
)
def test_a_head_is_its_pull_requests_where_a_comment_on_it_was_written_against_it(
    tmp_path: Path, repository: Path, read: bool, foreign: bool
) -> None:
    forge, head = _reviewed(tmp_path, repository, '## Review (combined)\n\nAt {head}.\n', ran=False)
    earlier = commit(repository, {NOTE: TEXT.replace('GPU', 'disk')})
    named = f'Read at {earlier[:12]}.' if read else f'Read at {head[:12]}; it rebuilds `{earlier[:12]}`.'
    _ = forge.say(PULL, f'## Review (round 1)\n\n{named}\n')
    _ruled(forge, PULL + 1, f'## Ruling\n\n- #{PULL} {NOTE}:9 at {earlier} [gate:stub clean]\n')
    stub = stub_gate(tmp_path)

    harvested = harvest(forge, HarvestGit(repository), [PULL + 1], [stub])

    assert [(c.head, c.line) for c in stub.cases()] == ([] if foreign else [(earlier, 9)])
    assert (Kind.FOREIGN in {note.kind for note in harvested.notes}) is foreign


def test_a_link_or_a_subscript_that_opens_with_gate_is_no_tag(tmp_path: Path, repository: Path) -> None:
    review = '## Review (combined)\n\nAt {head}: `gates: Sequence[Gate]`; see [gateway.md](gateway.md).\n'
    forge, _ = _reviewed(tmp_path, repository, review, ran=False)

    harvested = harvest(forge, HarvestGit(repository), [PULL], [stub_gate(tmp_path)])

    assert Kind.MALFORMED not in {note.kind for note in harvested.notes}
