"""`gate: narration`: its extractor over changes this repository's reviews ruled on.

The extractor cases read real changes, recorded under
`tests/data/gates/narration/`: the file a pull request wrote, at the head its
review read or the commit it merged as, with the lines the change writes
there. A long file is kept
from a line its passages do not cross (its first line, `first`), and the
case puts it back at its own line numbers. The commits of a pull request
whose messages a review ruled on are recorded under `commits/`, each with
the head the review read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import commit, scratch_repository

from kluster.scripts.dispatch.gates import gate
from kluster.scripts.dispatch.gates.extract import NOT_WRITTEN_HERE, Change, Changed, Item, change, message_path
from kluster.scripts.dispatch.gates.narration import GATE
from kluster.scripts.dispatch.prcheck import Commit, Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'narration'

#: The canon the gate quotes, as a scratch repository's tree holds it.
AGENTS = 'AGENTS.md'


#: Every change recorded under `DATA`, and every pull request whose commits are.
RECORDED = sorted(p.stem for p in DATA.glob('*.json'))
COMMITS = sorted(p.stem for p in (DATA / 'commits').glob('*.json'))


def recorded_change(name: str) -> Change:
    """The change recorded as `name`, its file at its own line numbers."""
    raw = cast(dict[str, Any], json.loads((DATA / f'{name}.json').read_text()))
    path = cast(str, raw['path'])
    text = '\n' * (cast(int, raw['first']) - 1) + cast(str, raw['text'])
    written = tuple((first, last) for first, last in cast(list[list[int]], raw['written']))
    return Change(
        head=cast(str, raw['head']),
        files=(Changed(path, 'M', written),),
        text=lambda asked: text if asked == path else None,
    )


def recorded_commits(name: str) -> Change:
    """The commits of the pull request recorded as `name`, at the head its review read, and no file."""
    raw = cast(dict[str, Any], json.loads((DATA / 'commits' / f'{name}.json').read_text()))
    commits = cast(list[dict[str, str]], raw['commits'])
    return Change(
        head=cast(str, raw['head']),
        files=(),
        text=lambda _: None,
        commits=tuple(Commit(c['sha'], c['message']) for c in commits),
    )


def narration() -> gate.Gate:
    return gate.named('narration')


@pytest.mark.parametrize(
    ('name', 'places'),
    [
        # A list item is one passage however it wraps, apart from the items nested under it.
        pytest.param('upkeep-role', [(364, 376, False), (377, 383, False), (384, 390, False)], id='markdown-list'),
        # The indented shell blocks (1147–1151, 1183–1188) give none, though the change writes 1149–1151;
        # a paragraph the change rewrites in part (1160 of 1153–1160) is an item of its written lines.
        pytest.param(
            'rebuild-runbook',
            [
                (1144, 1145, False),
                (1160, 1160, True),
                (1162, 1174, False),
                (1176, 1181, False),
                (1190, 1201, False),
                (1203, 1214, False),
                (1216, 1225, False),
                (1227, 1241, False),
                (1243, 1247, True),
                (1263, 1267, False),
                (1300, 1300, True),
                (1302, 1315, False),
            ],
            id='markdown-indented-code',
        ),
        # The fenced shell blocks (1497–1501, 1527–1535, 1562–1565, 1598–1604) give none.
        pytest.param(
            'overlay-step',
            [
                (1489, 1492, True),
                (1494, 1495, False),
                (1504, 1520, True),
                (1522, 1525, False),
                (1537, 1550, False),
                (1552, 1560, False),
                (1567, 1574, False),
                (1576, 1578, False),
                (1579, 1580, False),
                (1581, 1583, False),
                (1584, 1587, False),
                (1589, 1596, False),
                (1606, 1611, False),
            ],
            id='markdown-fenced-code',
        ),
        # A heading written straight after a list item's last line is a passage of its own.
        pytest.param('canary-heading', [(2741, 2741, False), (2743, 2765, False)], id='markdown-heading'),
        # One row the change rewrites is a passage of its own, not the whole table.
        pytest.param('health-table', [(1192, 1192, False)], id='markdown-table-row'),
        # Each paragraph of the module docstring, the first rewritten in part, and each run of `#:` comments;
        # the code between gives none.
        pytest.param(
            'quota-bounds',
            [
                (17, 22, True),
                (24, 31, False),
                (33, 34, False),
                (53, 54, False),
                (58, 63, False),
                (69, 69, True),
                (92, 94, True),
            ],
            id='python',
        ),
        # A paragraph of comment lines in a file of another kind ends at a line holding only its marker: the
        # change writes that line and the whole paragraph after it, which is an item with no more to carry.
        pytest.param('cilium-pin', [(198, 205, False)], id='comment-paragraph'),
        # A Jinja comment is one passage from its `{#` line to its `-#}` line, the prose between included.
        pytest.param('emailproxy-config', [(1, 6, False)], id='jinja-comment'),
    ],
)
def test_the_extractor_draws_one_item_for_each_passage_of_prose_a_change_writes(
    name: str, places: list[tuple[int, int, bool]]
) -> None:
    """Each place, and whether the change writes only part of its passage, which the item then carries whole."""
    assert [(i.line, i.end_line, bool(i.context)) for i in GATE.extract(recorded_change(name))] == places


def test_each_labelled_case_a_recorded_change_holds_is_what_the_extractor_draws_there() -> None:
    # Several recordings may hold one file at one head, so the items of all of them are gathered first.
    drawn: dict[tuple[str, str], list[Item]] = {}
    for recorded in [*map(recorded_change, RECORDED), *map(recorded_commits, COMMITS)]:
        for item in GATE.extract(recorded):
            drawn.setdefault((recorded.head, item.path), []).append(item)
    held = [case for case in narration().cases() if (case.head, case.path) in drawn]
    # The pair the set opens with is among them, the one the gate must catch, and so is every message's case.
    assert narration().cases()[0] in held
    assert {c.path for c in narration().cases() if c.path.startswith(':commit/')} <= {c.path for c in held}
    for case in held:
        assert case.item in drawn[(case.head, case.path)], case.item.place


def test_text_this_repository_holds_but_did_not_write_gives_no_item() -> None:
    comment = '# It used to be read by hand.\n'
    paths = [f'{prefix}held.sh' for prefix in NOT_WRITTEN_HERE] + ['scripts/written.sh']
    change = Change(
        head='0' * 40,
        files=tuple(Changed(path, 'A', ((1, 1),)) for path in paths),
        text=lambda _: comment,
    )

    assert [i.path for i in GATE.extract(change)] == ['scripts/written.sh']


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def test_each_paragraph_of_a_commits_body_is_an_item_at_the_place_that_names_the_commit(repository: Path) -> None:
    base = commit(repository, {AGENTS: 'Rules.\n'})
    message = (
        'docs: the cache note\n\n'
        'The note says where the cache runs.\n\n'
        'It was rewritten after the review\nasked for it.\n\n'
        'Co-Authored-By: Someone <someone@example.com>\n'
    )
    head = commit(repository, {'scripts/run.sh': 'true\n'}, message)

    items = GATE.extract(change(Git(repository), base, head, ''))

    # The subject and the trailers are no passage; each paragraph between them is one, whole, at its own lines.
    place = message_path(head)
    assert [(i.path, i.line, i.end_line, i.text) for i in items] == [
        (place, 3, 3, 'The note says where the cache runs.'),
        (place, 5, 6, 'It was rewritten after the review\nasked for it.'),
    ]


def test_a_paragraph_of_references_is_no_item_and_one_trailer_line_leaves_a_paragraph_prose() -> None:
    # 949's commit closes its body with `Refs Aetf/kluster-ops#917`, a paragraph of its own above the trailers.
    refs = recorded_commits('949-refs')
    (held,) = refs.commits
    line = held.message.split('\n').index('Refs Aetf/kluster-ops#917') + 1
    assert [i for i in GATE.extract(refs) if i.overlaps(line, line)] == []
    assert GATE.extract(refs)
    # A last paragraph is the trailers only where every line of it is one.
    noted = Commit('d' * 40, 'docs: the cache\n\nThe cache keeps a week.\nNote: a week is the default.\n')
    items = GATE.extract(Change(head='e' * 40, files=(), text=lambda _: None, commits=(noted,)))
    assert [(i.line, i.end_line) for i in items] == [(3, 4)]
