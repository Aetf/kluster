"""`gate: narration`: its extractor over changes this repository's reviews ruled on, and the gate run in shadow.

The extractor cases read real changes, recorded under
`tests/data/gates/narration/`: the file a pull request wrote, at the head its
review read or the commit it merged as, with the lines the change writes
there. A long file is kept
from a line its passages do not cross (its first line, `first`), and the
case puts it back at its own line numbers. The run case asks the gate as it
is declared, about the pair its labelled set opens with, and serves the
replies recorded beside the changes, so it reaches no network and holds no
key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import PULL, GateMemoryForge, Recorded, commit, pull_request, scratch_repository

from kluster.lib import workstation
from kluster.scripts.dispatch.gates import gate, run
from kluster.scripts.dispatch.gates.calibration import Status
from kluster.scripts.dispatch.gates.extract import NOT_WRITTEN_HERE, Change, Changed, Item
from kluster.scripts.dispatch.gates.narration import GATE
from kluster.scripts.dispatch.prcheck import Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'narration'

#: The canon the gate quotes, as a scratch repository's tree holds it.
AGENTS = 'AGENTS.md'


#: Every change recorded under `DATA`.
RECORDED = sorted(p.stem for p in DATA.glob('*.json'))


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
        # The module docstring, rewritten in part, and each run of `#:` comments; the code between gives none.
        pytest.param(
            'quota-bounds',
            [(17, 34, True), (53, 54, False), (58, 63, False), (69, 69, True), (92, 94, True)],
            id='python',
        ),
        # A run of comment lines in a file of another kind, written into the end of a longer run.
        pytest.param('cilium-pin', [(197, 205, True)], id='comment-run'),
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
    for change in map(recorded_change, RECORDED):
        drawn.setdefault((change.head, change.files[0].path), []).extend(GATE.extract(change))
    held = [case for case in narration().cases() if (case.head, case.path) in drawn]
    # The pair the set opens with is among them: the one the gate must catch.
    assert narration().cases()[0] in held
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


def test_the_gate_runs_in_shadow_end_to_end_on_its_falsifier_pair_against_recorded_replies(repository: Path) -> None:
    gate_ = narration()
    catch, keep = gate_.cases()[:2]
    canon = (workstation.repo_root() / AGENTS).read_text()
    base = commit(repository, {AGENTS: canon})
    head = commit(repository, {'docs/archive.md': f'{catch.text}\n\n{keep.text}\n'})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    replies = Recorded([cast(dict[str, Any], json.loads(p.read_text())) for p in sorted((DATA / 'messages').iterdir())])

    posted = run.review(forge, Git(repository), PULL, [gate_], replies.model)

    # Each sample carries the rule as the head's AGENTS.md states it, and both passages as data.
    assert len(replies.requests) == 3
    section = gate_.canon(lambda path: canon if path == AGENTS else None).text
    for request in replies.requests:
        assert section in request['system']
        rows = cast(list[dict[str, Any]], json.loads(request['messages'][0]['content'].partition('\n\n')[2]))
        assert [row['text'] for row in rows] == [catch.text, keep.text]
    assert posted is not None
    (ran,) = posted.evidence.gates
    assert ran.gate == 'narration'
    assert ran.status is Status.SHADOW
    # A shadow finding blocks nothing.
    assert not run.refuses(0, posted)
