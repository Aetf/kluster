"""`dispatch claim` and `queue` ask the model gates that read briefs about the brief, once its preconditions pass.

The gates are this repository's `enumeration` and `conclusion`, their canon
read from the checkout, and the model a recorded reply that finds one line
of the brief. Each gate is shown the brief whole. Its findings land in the
dispatcher's log and the claim goes ahead, whatever status the gate's
calibration earns: a record that earns `blocking`, written here against a
copy of the gate, refuses nothing, and neither does such a gate where no
model can be asked, its canonical section does not read, or its samples
leave the brief undecided, each of which the log names. A gate whose
trigger names no brief, as a trigger of every path does not, asks nothing
about one. The command line hands the claim the brief's file, so
`dispatch claim` asks the gates as the step does. The rest of the claim's preconditions are `test_dispatch_ledger.py`'s.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from gate_stub import Recorded, reply
from memory_forge import MemoryForge

from kluster.lib import workstation
from kluster.scripts.dispatch import brief, claim, cli, ledger, serialized
from kluster.scripts.dispatch.claim import Asking, BriefFile
from kluster.scripts.dispatch.forge import Milestone
from kluster.scripts.dispatch.gates import enumeration
from kluster.scripts.dispatch.gates.calibration import Calibration, Status
from kluster.scripts.dispatch.gates.gate import RECORD, Gate, named
from kluster.scripts.dispatch.gates.model import KeyMissing, Model

PARALLEL = 'Parallel — non-blocking'
ISSUE = 917
SESSION = 'session-a'
#: Where the claim shows the brief of an issue in `area/cluster`, and the line of it the recorded reply finds.
SHOWN = ':brief/cluster/b-917-demo.md'
FOUND = 10

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


def _replies() -> Recorded:
    return Recorded([reply([{'path': SHOWN, 'line': FOUND, 'end_line': FOUND, 'why': 'It pins the pods to a node.'}])])


def _asking(*gates: Gate, model: Callable[[], Model] | None = None) -> Asking:
    return Asking(gates or (named('enumeration'), named('conclusion')), _checkout, model or _replies().model)


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


def _no_key() -> Model:
    raise KeyMissing('no model gates key on this machine')


def test_a_shadow_gates_findings_on_the_brief_are_logged_and_the_claim_goes_ahead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forge = _forge()
    replies = _replies()

    with caplog.at_level(logging.WARNING):
        verdict = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), _asking(model=replies.model))

    # Each gate is shown the brief whole, its front matter included.
    assert replies.requests
    assert all(line in r['messages'][0]['content'] for r in replies.requests for line in TEXT.splitlines())
    assert verdict.brief_evidence is not None
    found = {(r.gate, r.status, e.place) for r in verdict.brief_evidence.gates for e in r.findings}
    assert found == {
        ('enumeration', Status.SHADOW, f'{SHOWN}:{FOUND}'),
        ('conclusion', Status.SHADOW, f'{SHOWN}:{FOUND}'),
    }
    assert f'{SHOWN}:{FOUND}' in caplog.text
    assert ledger.CLAIMED in forge.issues_[ISSUE].labels


def test_the_brief_of_an_issue_outside_the_cluster_area_is_not_the_conclusion_gates() -> None:
    verdict = claim.claim(_forge('area/dns', 'kind/task'), _dispatch(), ISSUE, serialized.read(), _asking())

    assert verdict.brief_evidence is not None
    assert [r.gate for r in verdict.brief_evidence.gates] == ['enumeration']


@pytest.mark.parametrize('step', ['claim', 'queue'])
def test_a_blocking_gates_finding_on_the_brief_is_logged_and_refuses_nothing(
    tmp_path: Path, step: str, caplog: pytest.LogCaptureFixture
) -> None:
    forge = _forge()
    asking = _asking(_blocking(tmp_path))

    with caplog.at_level(logging.WARNING):
        if step == 'claim':
            verdict = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), asking)
        else:
            verdict = claim.queue(forge, _dispatch(), ISSUE, serialized.read(), TEXT, asking)

    assert verdict.brief_evidence is not None
    assert [(r.status, [e.place for e in r.findings]) for r in verdict.brief_evidence.gates] == [
        (Status.BLOCKING, [f'{SHOWN}:{FOUND}'])
    ]
    assert f'enumeration, blocking: {SHOWN}:{FOUND}' in caplog.text
    assert not forge.issues_[ISSUE].labels.isdisjoint(ledger.CLAIMS)


def test_where_no_model_can_be_asked_the_gates_ask_nothing_and_the_claim_goes_ahead(tmp_path: Path) -> None:
    shadow = claim.claim(_forge(), _dispatch(), ISSUE, serialized.read(), _asking(model=_no_key))
    blocking = claim.claim(_forge(), _dispatch(), ISSUE, serialized.read(), _asking(_blocking(tmp_path), model=_no_key))

    assert shadow.brief_evidence is not None
    assert [(r.gate, r.asked) for r in shadow.brief_evidence.gates] == [('enumeration', False), ('conclusion', False)]
    assert blocking.brief_evidence is not None
    assert [(r.status, r.asked) for r in blocking.brief_evidence.gates] == [(Status.BLOCKING, False)]


def test_a_place_a_blocking_gate_left_undecided_on_the_brief_is_logged_and_the_claim_goes_ahead(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    unread = {**reply([]), 'content': [{'type': 'text', 'text': 'not a reply in the schema'}]}
    forge = _forge()
    model = Recorded([*_replies().bodies, unread, unread]).model

    with caplog.at_level(logging.WARNING):
        verdict = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), _asking(_blocking(tmp_path), model=model))

    assert verdict.brief_evidence is not None
    assert f'enumeration, blocking: {SHOWN}:{FOUND}: no verdict' in caplog.text
    assert f'enumeration, blocking: {SHOWN}:1–' in caplog.text
    assert ledger.CLAIMED in forge.issues_[ISSUE].labels


def test_a_blocking_gate_whose_canon_does_not_read_logs_that_it_asked_nothing_and_the_claim_goes_ahead(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    forge = _forge()

    def unread(_: str) -> str | None:
        return None

    asking = replace(_asking(_blocking(tmp_path)), canon=unread)

    with caplog.at_level(logging.INFO):
        verdict = claim.claim(forge, _dispatch(), ISSUE, serialized.read(), asking)

    assert verdict.brief_evidence is not None
    assert 'enumeration on the brief, blocking: asked nothing: its canonical section does not read' in caplog.text
    assert ledger.CLAIMED in forge.issues_[ISSUE].labels


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

    assert f'enumeration, shadow: {SHOWN}:{FOUND}' in caplog.text
    assert ledger.CLAIMED in forge.issues_[ISSUE].labels


def test_a_gate_whose_trigger_names_no_brief_asks_nothing_about_one(tmp_path: Path) -> None:
    asking = _asking(named('enumeration'), _caught_all(tmp_path))

    verdict = claim.claim(_forge(), _dispatch(), ISSUE, serialized.read(), asking)

    assert verdict.brief_evidence is not None
    assert [r.gate for r in verdict.brief_evidence.gates] == ['enumeration']
