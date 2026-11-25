"""A gate's calibration: the record `calibrate` writes, the status it earns, and the record held to its gate.

The first case is the relation every gate's record holds against the gate
the tree defines: a record measures one prompt, one canonical section and
one model, so an edit to any of them leaves it measuring something else. The
two halves are of independent origin, the gate's files edited by hand and
the record written by a measurement of them, the way `mise.lock` is held
against `mise.toml`; the stub gate's case below turns it red on each of
those edits. The rest run the stub gate (`gate_stub`) against a model that
answers from a script.
"""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import RULE, RULE_DOCUMENT, SPEC, stub_gate

from kluster.lib import workstation
from kluster.scripts.dispatch.gates import ask, gate
from kluster.scripts.dispatch.gates.calibrate import calibrate
from kluster.scripts.dispatch.gates.calibration import Status, standing
from kluster.scripts.dispatch.gates.cases import Case
from kluster.scripts.dispatch.gates.gate import CASES, Gate
from kluster.scripts.dispatch.gates.model import Model, Reply, Request
from kluster.scripts.dispatch.gates.verdict import Verdict


def test_every_gates_record_measures_the_gate_as_the_tree_holds_it() -> None:
    checkout = workstation.repo_root()

    def text(path: str) -> str | None:
        file = checkout / path
        return file.read_text() if file.is_file() else None

    stale = {g.name: changed for g in gate.discover() if (changed := g.stale(text))}

    # A gate named here runs in shadow until `dispatch gates calibrate <name>` measures it again.
    assert stale == {}


@pytest.mark.parametrize(
    ('part', 'said'),
    [
        ('prompt', 'the prompt'),
        ('harness', 'the prompt'),
        ('counts', 'the prompt'),
        ('canon', 'the canonical text'),
        ('model', 'the model'),
    ],
)
def test_a_record_whose_hash_mismatches_reads_as_shadow_and_says_why(
    tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch, part: str, said: str
) -> None:
    stub = stub_gate(tmp_path)
    _ = _set(stub)

    def model() -> Model:
        return Scripted()

    _ = calibrate(stub, checkout, model)
    if part == 'prompt':
        _ = (stub.directory / gate.PROMPT).write_text(f'{stub.prompt}\nAnswer briefly.\n')
    elif part == 'harness':
        monkeypatch.setattr(ask, 'FRAME', f'{ask.FRAME} Answer briefly.')
    elif part == 'counts':
        monkeypatch.setattr(ask, 'MAJORITY', ask.MAJORITY - 1)
    elif part == 'canon':
        _ = (checkout / RULE_DOCUMENT).write_text(RULE.replace('never where it runs', 'never where it lives'))
    else:
        stub = replace(stub, spec=replace(stub.spec, model='claude-sonnet-5-5'))

    def text(path: str) -> str | None:
        return (checkout / path).read_text()

    status, why = standing(stub.record(), stub.hashes(stub.canon(text).text))

    assert stub.stale(text) == [said]
    assert status is Status.SHADOW
    assert why.startswith(f'{said} changed since the calibration record was measured')


def test_discovery_finds_each_directory_holding_a_prompt_and_refuses_one_with_no_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, 'path', [str(tmp_path), *sys.path])
    module = f'scratch_gates_{tmp_path.name.replace("-", "_")}'
    package = tmp_path / module
    package.mkdir()
    _ = (package / '__init__.py').write_text('')
    _ = stub_gate(package)
    _ = (package / 'stub' / '__init__.py').write_text('from gate_stub import SPEC as GATE\n')
    _ = (package / 'notes').mkdir()

    (found,) = gate.discover(package, module)

    assert (found.name, found.spec) == ('stub', SPEC)
    unread = package / 'unread_gate'
    unread.mkdir()
    _ = (unread / gate.PROMPT).write_text('{canon}\n')
    _ = (unread / '__init__.py').write_text('')
    with pytest.raises(gate.GateUnreadable, match=f'{module}.unread_gate declares no GATE'):
        _ = gate.discover(package, module)


@dataclass
class Scripted:
    """A model that calls a line naming a node a finding and any other a pass, except where told to slip."""

    #: How many times to answer `pass` to the line `slip` names, before answering as scripted.
    slips: int = 0
    slip: str = ''
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def ask(self, request: Request) -> Reply:
        rows = cast(list[dict[str, Any]], json.loads(request.user.split('\n\n', 1)[1]))
        answers: list[dict[str, object]] = []
        for row in rows:
            finding = 'node' in str(row['text'])
            with self._lock:
                if finding and self.slips and row['text'] == self.slip:
                    self.slips -= 1
                    finding = False
            answers.append({'id': row['id'], 'verdict': 'finding' if finding else 'pass', 'why': 'scripted'})
        return Reply(json.dumps({'items': answers}))


def _set(stub: Gate) -> list[Case]:
    """Eight positives and eight negatives, the falsifier pair first, written as the stub gate's cases."""
    cases = [
        Case(900 + n, 'a' * 40, 'docs/notes/a.md', n, n, verdict, f'https://example.invalid/{n}', text)
        for n, (verdict, text) in enumerate(
            [(Verdict.FINDING, 'It runs on node 0.'), (Verdict.PASS, 'It requests a GPU 0.')]
            + [(Verdict.FINDING, f'It runs on node {n}.') for n in range(1, 8)]
            + [(Verdict.PASS, f'It requests a GPU {n}.') for n in range(1, 8)],
            start=1,
        )
    ]
    _ = (stub.directory / CASES).write_text(''.join(f'{c.to_json()}\n' for c in cases))
    return cases


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout holding the stub gate's canon."""
    root = tmp_path / 'checkout'
    (root / 'docs').mkdir(parents=True)
    _ = (root / RULE_DOCUMENT).write_text(RULE)
    return root


def test_a_gate_that_meets_the_bar_earns_blocking_and_runs_blocking(tmp_path: Path, checkout: Path) -> None:
    stub = stub_gate(tmp_path)
    _ = _set(stub)

    def model() -> Model:
        return Scripted()

    record = calibrate(stub, checkout, model)

    assert (record.status, record.recall, record.false_positives, record.pair) == (Status.BLOCKING, 1.0, 0.0, True)
    assert stub.record() == record
    assert standing(stub.record(), stub.hashes(stub.canon(lambda _: RULE).text))[0] is Status.BLOCKING


def test_a_falsifier_pair_that_slips_in_one_sample_keeps_the_gate_in_shadow(tmp_path: Path, checkout: Path) -> None:
    stub = stub_gate(tmp_path)
    _ = _set(stub)
    scripted = Scripted(slips=1, slip='It runs on node 0.')

    def model() -> Model:
        return scripted

    record = calibrate(stub, checkout, model)

    # Two samples of three still find it, and recall over the set is whole: the pair alone keeps it out.
    assert (record.status, record.recall, record.pair) == (Status.SHADOW, 1.0, False)
    status, why = standing(record, stub.hashes(stub.canon(lambda _: RULE).text))
    assert status is Status.SHADOW
    assert 'its first two cases' in why


def test_a_set_short_of_the_minimum_earns_no_status_whatever_it_scores(tmp_path: Path, checkout: Path) -> None:
    stub = stub_gate(tmp_path)
    cases = _set(stub)[:4]
    _ = (stub.directory / CASES).write_text(''.join(f'{c.to_json()}\n' for c in cases))

    def model() -> Model:
        return Scripted()

    record = calibrate(stub, checkout, model)

    assert (record.status, record.recall, record.pair) == (Status.SHADOW, 1.0, True)
    assert record.shortfalls() == ['the set holds 3 positives and 1 negatives, where the bar measures on 8 of each']
