"""`dispatch claim` and `queue` ask the model gates that read briefs about the brief, once its preconditions pass.

The gates are this repository's `enumeration` and `conclusion`, their canon
read from the checkout, and the model a recorded reply that finds the one
item each gate draws from the brief. With no calibration record a gate is in
shadow: its finding lands in the dispatcher's log and the claim goes ahead.
A gate whose record earns `blocking`, written here against a copy of the
gate, refuses the claim on the same finding, on an item its samples left
undecided, wherever it asks nothing, and where its canonical section does
not read, with nothing on the forge changed. A gate whose trigger names no
brief, as a trigger of every path does not, asks nothing about one. The
command line hands the claim the brief's file, so `dispatch claim` asks the
gates as the step does. The rest of the claim's preconditions are
`test_dispatch_ledger.py`'s.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import Recorded
from memory_forge import MemoryForge

from kluster.lib import workstation
from kluster.scripts.dispatch import brief, claim, cli, ledger, serialized
from kluster.scripts.dispatch.claim import Asking, BriefFile, Precondition
from kluster.scripts.dispatch.forge import Milestone
from kluster.scripts.dispatch.gates import enumeration
from kluster.scripts.dispatch.gates.calibration import Calibration, Status
from kluster.scripts.dispatch.gates.gate import RECORD, Gate, named
from kluster.scripts.dispatch.gates.model import KeyMissing, Model

PARALLEL = 'Parallel — non-blocking'
ISSUE = 917
SESSION = 'session-a'
REPLY = Path(__file__).parent / 'data' / 'gates' / 'enumeration' / 'messages' / '1.json'

#: A brief whose prose holds one sentence of each gate's kind: an entry to add, and a node to run on.
TEXT = """---
issue: 917
role: builder
done: The component merges and the gate passes.
owned: [src/kluster/components/apps/demo/]
closes: true
workspace: b-917-demo
---
Write the component. Add its name to the census in `tests/test_census.py`.
Pin its pods to the worker node with a node selector.
"""


def _checkout(path: str) -> str | None:
    file = workstation.repo_root() / path
    return file.read_text() if file.is_file() else None


def _asking(*gates: Gate, model: Callable[[], Model] | None = None) -> Asking:
    replies = Recorded([cast(dict[str, Any], json.loads(REPLY.read_text()))])
    return Asking(gates or (named('enumeration'), named('conclusion')), _checkout, model or replies.model)


def _forge(*labels: str) -> MemoryForge:
    forge = MemoryForge()
    forge.milestones_[PARALLEL] = Milestone(PARALLEL, open=True, description='Beside the mainline.')
    forge.add(ISSUE, PARALLEL, *(labels or ('area/cluster', 'kind/task')))
    return forge


def _dispatch() -> claim.Dispatch:
    return claim.Dispatch(brief=brief.read(TEXT.encode()), session=SESSION, file=BriefFile('b-917-demo.md', TEXT))


def _blocking(tmp_path: Path) -> Gate:
    """A copy of `enumeration` whose calibration record earns `blocking` for it as the checkout holds it."""
    copy = tmp_path / 'enumeration'
    _ = shutil.copytree(Path(enumeration.__file__).parent, copy, ignore=shutil.ignore_patterns('__pycache__'))
    gate = Gate.load(copy, enumeration.GATE)
    held = gate.hashes(gate.canon(_checkout).text)
    measured = Calibration(held, 'unread here', 8, 8, 1.0, 0.0, 0.0, pair=True)
    _ = (copy / RECORD).write_text(measured.to_json())
    assert gate.record() == measured
    return gate


def _caught_all(tmp_path: Path) -> Gate:
    """A copy of `enumeration` whose trigger takes every path a pull request can touch."""
    copy = tmp_path / 'everything'
    _ = shutil.copytree(Path(enumeration.__file__).parent, copy, ignore=shutil.ignore_patterns('__pycache__'))
    return Gate.load(copy, replace(enumeration.GATE, trigger=('**',)))


def _silent(reply: dict[str, Any]) -> dict[str, Any]:
    """`reply` answering no item."""
    return {**reply, 'content': [{'type': 'text', 'text': json.dumps({'items': []})}]}


def _no_key() -> Model:
    raise KeyMissing('no model gates key on this machine')


def test_a_shadow_gates_findings_on_the_brief_are_logged_and_the_claim_goes_ahead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forge = _forge()

    with caplog.at_level(logging.WARNING):
        verdict = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), _asking())

    assert verdict.brief_evidence is not None
    found = {(r.gate, r.status, e.place) for r in verdict.brief_evidence.gates for e in r.findings}
    assert found == {
        ('enumeration', Status.SHADOW, ':brief/cluster/b-917-demo.md:9'),
        ('conclusion', Status.SHADOW, ':brief/cluster/b-917-demo.md:10'),
    }
    assert ':brief/cluster/b-917-demo.md:9' in caplog.text
    assert ledger.CLAIMED in forge.issues_[ISSUE].labels


def test_the_brief_of_an_issue_outside_the_cluster_area_is_not_the_conclusion_gates() -> None:
    verdict = claim.claim(_forge('area/dns', 'kind/task'), _dispatch(), ISSUE, serialized.read(), _asking())

    assert verdict.brief_evidence is not None
    assert [r.gate for r in verdict.brief_evidence.gates] == ['enumeration']


@pytest.mark.parametrize('step', ['claim', 'queue'])
def test_a_blocking_gates_finding_on_the_brief_refuses_and_changes_nothing(tmp_path: Path, step: str) -> None:
    forge = _forge()
    asking = _asking(_blocking(tmp_path))

    with pytest.raises(claim.Refused) as refused:
        if step == 'claim':
            _ = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), asking)
        else:
            _ = claim.queue(forge, _dispatch(), ISSUE, serialized.read(), TEXT, asking)

    assert [r.precondition for r in refused.value.refusals] == [Precondition.BRIEF_GATE]
    assert ':brief/cluster/b-917-demo.md:9' in str(refused.value)
    assert forge.issues_[ISSUE].comments == []
    assert forge.issues_[ISSUE].labels.isdisjoint(ledger.CLAIMS)


def test_where_no_model_can_be_asked_a_blocking_gate_refuses_and_a_shadow_one_lets_the_claim_through(
    tmp_path: Path,
) -> None:
    shadow = claim.claim(_forge(), _dispatch(), ISSUE, serialized.read(), _asking(model=_no_key))
    assert shadow.brief_evidence is not None
    assert [(r.gate, r.asked) for r in shadow.brief_evidence.gates] == [('enumeration', False), ('conclusion', False)]

    with pytest.raises(claim.Refused) as refused:
        _ = claim.claim(_forge(), _dispatch(), ISSUE, serialized.read(), _asking(_blocking(tmp_path), model=_no_key))
    assert [r.precondition for r in refused.value.refusals] == [Precondition.BRIEF_GATE]


def test_the_command_line_hands_the_claim_the_brief_and_the_gates_ask_about_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    forge = _forge()
    written = tmp_path / 'b-917-demo.md'
    _ = written.write_text(TEXT)
    monkeypatch.setattr(cli, 'Gh', lambda: forge)
    monkeypatch.setattr(claim, 'workstation_asking', _asking)
    monkeypatch.setenv(cli.SESSION, SESSION)

    with caplog.at_level(logging.WARNING):
        assert cli.main(['claim', str(ISSUE), str(written)]) == cli.DONE

    assert 'enumeration, shadow: :brief/cluster/b-917-demo.md:9' in caplog.text
    assert ledger.CLAIMED in forge.issues_[ISSUE].labels


def test_a_gate_whose_trigger_names_no_brief_asks_nothing_about_one(tmp_path: Path) -> None:
    asking = _asking(named('enumeration'), _caught_all(tmp_path))

    verdict = claim.claim(_forge(), _dispatch(), ISSUE, serialized.read(), asking)

    assert verdict.brief_evidence is not None
    assert [r.gate for r in verdict.brief_evidence.gates] == ['enumeration']


def test_an_item_a_blocking_gate_left_undecided_on_the_brief_refuses_the_claim(tmp_path: Path) -> None:
    reply = cast(dict[str, Any], json.loads(REPLY.read_text()))
    forge = _forge()

    with pytest.raises(claim.Refused) as refused:
        _ = claim.claim(
            forge,
            _dispatch(),
            ISSUE,
            serialized.read(),
            _asking(_blocking(tmp_path), model=Recorded([reply, _silent(reply), _silent(reply)]).model),
        )

    assert [r.precondition for r in refused.value.refusals] == [Precondition.BRIEF_GATE]
    assert 'no verdict' in str(refused.value)
    assert forge.issues_[ISSUE].comments == []


def test_a_blocking_gate_whose_canon_does_not_read_refuses_the_claim(tmp_path: Path) -> None:
    forge = _forge()

    def unread(_: str) -> str | None:
        return None

    asking = replace(_asking(_blocking(tmp_path)), canon=unread)

    with pytest.raises(claim.Refused) as refused:
        _ = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), asking)

    assert [r.precondition for r in refused.value.refusals] == [Precondition.BRIEF_GATE]
    assert forge.issues_[ISSUE].comments == []
