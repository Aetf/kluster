"""`gate: conclusion`: its extractor on reviewed changes and on a brief, and the gate run end to end in shadow.

Each fixture under `tests/data/gates/conclusion/` is one file of a reviewed
pull request: its text at the head the review read, and the lines the pull
request wrote there. Every case of the gate's labelled set drawn from that
file is an item the extractor draws from that change, its text and context
included, since a case is what the gate would have been asked
(`gates.cases`); and every item is of a line the change writes. A brief is
the gate's only where its issue is in `area/cluster`, beside any other area.
The run end to end is the harness's own, with a recorded reply standing in
for the model and no calibration record, so the gate is discovered, its
canon read and its item asked about in shadow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import GateMemoryForge, Recorded, commit, pull_request, scratch_repository

from kluster.lib import workstation
from kluster.scripts.dispatch.gates import run
from kluster.scripts.dispatch.gates.conclusion import extract
from kluster.scripts.dispatch.gates.extract import Change, Changed
from kluster.scripts.dispatch.gates.gate import named
from kluster.scripts.dispatch.gates.reading import brief_path
from kluster.scripts.dispatch.prcheck import Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'conclusion'
GATE = named('conclusion')


def _change(fixture: Path) -> tuple[dict[str, Any], Change]:
    """A fixture's record, and the change it holds: one file, the lines it writes, its text at the head."""
    held = cast(dict[str, Any], json.loads(fixture.read_text()))
    path, text = str(held['path']), str(held['text'])
    written = tuple((int(first), int(last)) for first, last in cast(list[list[int]], held['written']))
    changed = Changed(path, str(held['kind']), written)
    return held, Change(head=str(held['head']), files=(changed,), text=lambda asked: text if asked == path else None)


@pytest.mark.parametrize('fixture', sorted(DATA.glob('*.json')), ids=lambda p: p.stem)
def test_each_case_of_a_reviewed_change_is_an_item_the_extractor_draws_from_it(fixture: Path) -> None:
    held, change = _change(fixture)
    drawn = extract(change)
    cases = [c for c in GATE.cases() if (c.pr, c.head, c.path) == (held['pr'], held['head'], held['path'])]

    assert cases, f'{fixture.name} holds no case of the set'
    by_place = {(i.path, i.line, i.end_line): i for i in drawn}
    assert [by_place.get((c.path, c.line, c.end_line)) for c in cases] == [c.item for c in cases]
    (changed,) = change.files
    assert all(any(i.overlaps(first, last) for first, last in changed.written) for i in drawn)


BRIEF = """---
issue: 897
role: builder
done: The mover is held where the workload runs.
owned: [src/kluster/components/apps/storage.py]
closes: true
---
The mover takes the workload's node constraints. Write the test first.
"""


@pytest.mark.parametrize(
    ('areas', 'asked'),
    [({'cluster'}, True), ({'ci', 'cluster'}, True), ({'cluster', 'dns'}, True), ({'dns'}, False)],
    ids=['cluster', 'cluster-second', 'cluster-first', 'dns'],
)
def test_a_brief_is_the_gates_where_its_issue_is_in_the_cluster_area(areas: set[str], asked: bool) -> None:
    path = brief_path(areas, 'b-897.md')
    change = Change(head='0' * 64, files=(Changed(path, 'A', ((1, 9),)),), text=lambda _: BRIEF)

    assert GATE.triggered([path]) is asked
    assert [i.text for i in extract(change)] == (["The mover takes the workload's node constraints."] if asked else [])


def test_the_gate_runs_end_to_end_on_a_pull_request_in_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = workstation.repo_root()
    repository = scratch_repository(tmp_path, monkeypatch)
    canon = GATE.spec.canon.document
    base = commit(repository, {canon: (checkout / canon).read_text()})
    placed = (
        "from kluster.lib.k8s import Constraints\nPLACEMENT = {'nodeSelector': {'kubernetes.io/hostname': 'worker'}}\n"
    )
    head = commit(repository, {'src/kluster/components/apps/demo/__init__.py': placed})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    replies = Recorded([cast(dict[str, Any], json.loads((DATA / 'messages' / '1.json').read_text()))])

    posted = run.review(forge, Git(repository), 4917, [GATE], replies.model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert ran.gate == 'conclusion'
    assert [e.place for e in ran.entries] == ['src/kluster/components/apps/demo/__init__.py:2']
    assert 'The program declares constraints, not conclusions' in replies.requests[0]['system']


def _written(path: str, lines: list[str]) -> Change:
    """A change that writes `lines` whole, as the file at `path`."""
    text = '\n'.join(lines) + '\n'
    return Change(head='0' * 40, files=(Changed(path, 'A', ((1, len(lines)),)),), text=lambda _: text)


def test_a_long_def_header_is_asked_about_in_its_parameters_and_its_body_once() -> None:
    parameters = [f'    p{n}: int,' for n in range(20)]
    lines = [
        'def database(',
        *parameters,
        '    constraints: Constraints,',
        ') -> None:',
        '    """A database."""',
        "    pod = {'nodeSelector': constraints.labels}",
    ]

    drawn = extract(_written('src/kluster/components/apps/database.py', lines))

    assert [(i.line, i.end_line) for i in drawn] == [(22, 22), (25, 25)]


def test_a_long_part_with_no_smaller_part_is_asked_about_beside_a_short_one() -> None:
    values = [f'    key{n}: value  # tolerations' for n in range(22)]
    lines = [
        'POD = PodSpecArgs(',
        "    node_selector={'kubernetes.io/arch': 'amd64'},",
        '    annotations="""',
        *values,
        '""",',
        ')',
    ]

    drawn = extract(_written('src/kluster/components/apps/demo.py', lines))

    assert [(i.line, i.end_line) for i in drawn] == [(2, 2), *((n, n) for n in range(4, 26))]
