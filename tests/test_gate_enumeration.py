"""`gate: enumeration`: its extractor on reviewed changes and on a brief, and the gate run end to end in shadow.

Each fixture under `tests/data/gates/enumeration/` is one file of a reviewed
pull request: its text at the head the review read, and the lines the pull
request wrote there. Every case of the gate's labelled set drawn from that
file is an item the extractor draws from that change, its text and context
included, since a case is what the gate would have been asked
(`gates.cases`); and every item is of a line the change writes. The run end
to end is the harness's own, `dispatch gates <pr>` over a scratch
repository, with a recorded reply standing in for the model and no
calibration record, so the gate is discovered, its canon read and its
item asked about in shadow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import GateMemoryForge, Recorded, commit, pull_request, scratch_repository

from kluster.lib import workstation
from kluster.scripts.dispatch.gates import run
from kluster.scripts.dispatch.gates.enumeration import extract
from kluster.scripts.dispatch.gates.extract import Change, Changed
from kluster.scripts.dispatch.gates.gate import named
from kluster.scripts.dispatch.gates.reading import brief_path
from kluster.scripts.dispatch.prcheck import Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'enumeration'
GATE = named('enumeration')


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
issue: 917
role: builder
done: The census holds the new component.
owned: [tests/test_census.py]
closes: true
---
# Brief: the census

Write the component. Add its name to the
census in `tests/test_census.py`, beside the others. Its placement follows
from what it declares.
"""


def test_a_brief_sentence_that_tells_the_agent_to_add_an_entry_is_drawn_at_its_lines() -> None:
    path = brief_path({'cluster'}, 'b-917.md')
    change = Change(head='0' * 64, files=(Changed(path, 'A', ((1, 14),)),), text=lambda _: BRIEF)

    drawn = extract(change)

    assert [(i.line, i.end_line, i.text) for i in drawn] == [
        (10, 11, 'Add its name to the\ncensus in `tests/test_census.py`, beside the others.')
    ]


def test_the_gate_runs_end_to_end_on_a_pull_request_in_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = workstation.repo_root()
    repository = scratch_repository(tmp_path, monkeypatch)
    canon = GATE.spec.canon.document
    base = commit(repository, {canon: (checkout / canon).read_text()})
    head = commit(repository, {'tests/test_census.py': "NAMES = ('authelia', 'blog', 'immich')\n"})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    replies = Recorded([cast(dict[str, Any], json.loads((DATA / 'messages' / '1.json').read_text()))])

    posted = run.review(forge, Git(repository), 4917, [GATE], replies.model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert ran.gate == 'enumeration'
    assert [e.place for e in ran.entries] == ['tests/test_census.py:1']
    assert 'A membership is a definition, not a list' in replies.requests[0]['system']


def _written(files: dict[str, str]) -> Change:
    """A change that writes each of `files` whole."""
    changed = tuple(Changed(path, 'A', ((1, text.count('\n')),)) for path, text in files.items())
    return Change(head='0' * 40, files=changed, text=files.get)


SET = "NAMES = ('authelia', 'blog', 'immich')\n"


def test_text_held_here_but_written_elsewhere_gives_no_item() -> None:
    change = _written({'tests/data/gates/x/recorded.py': SET, 'tests/vendor/lib/mod.py': SET, 'tests/test_x.py': SET})

    assert [i.path for i in extract(change)] == ['tests/test_x.py']


def test_a_display_is_a_membership_where_a_statement_defines_or_parametrizes_a_set_with_it() -> None:
    lines = [
        'import os, sys',
        "@pytest.mark.parametrize('name', ['authelia', 'blog'])",
        'def test_a(name: str) -> None:',
        "    sp.run(['git', 'log', '--oneline'], check=True)",
        "    root.type, root.mode = 'dir', 0o755",
        "    assert name in ('authelia', 'blog')",
    ]

    drawn = extract(_written({'tests/test_x.py': '\n'.join(lines) + '\n'}))

    assert [i.line for i in drawn] == [2, 6]
