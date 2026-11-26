"""A gate's calibration: the record `calibrate` writes over a history, the status it earns, and the record held to its gate.

The first case is the relation every gate's record holds against the gate
the tree defines: a record measures one prompt, one canonical section and
one model, so an edit to any of them leaves it measuring something else. The
two halves are of independent origin, the gate's files edited by hand and
the record written by a measurement of them, the way `mise.lock` is held
against `mise.toml`; the stub gate's case below turns it red on each of
those edits.

The rest run the stub gate (`gate_stub`) over a history of pull requests in
a scratch repository, each writing a note of two lines, the first stating
where something runs and the second what it requests, with a case at each:
a finding on the first and a pass on the second. The model answers from a
script, finding each line that names a node, except where told otherwise.
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from gate_stub import RULE, RULE_DOCUMENT, SPEC, GateMemoryForge, commit, scratch_repository, stub_gate

from kluster.lib import workstation
from kluster.scripts.dispatch import cli
from kluster.scripts.dispatch.forge import ForgeError
from kluster.scripts.dispatch.gates import ask, gate, run
from kluster.scripts.dispatch.gates.calibrate import Unanswered, Unconfirmed, Unmeasured, calibrate, score
from kluster.scripts.dispatch.gates.calibration import (
    FALSE_FINDINGS,
    FINDINGS,
    PULL_REQUESTS,
    RECALL,
    Calibration,
    Status,
    standing,
)
from kluster.scripts.dispatch.gates.cases import Case
from kluster.scripts.dispatch.gates.comment import Entry
from kluster.scripts.dispatch.gates.forge import GateGh
from kluster.scripts.dispatch.gates.gate import CASES, RECORD, Gate
from kluster.scripts.dispatch.gates.harvest import HarvestGit, harvest
from kluster.scripts.dispatch.gates.model import Model, Reply, Request
from kluster.scripts.dispatch.gates.verdict import Answer, Verdict
from kluster.scripts.dispatch.prcheck import Git, PullRequest, PullUnreadable


def test_every_gates_record_measures_the_gate_as_the_tree_holds_it() -> None:
    checkout = workstation.repo_root()

    def text(path: str) -> str | None:
        file = checkout / path
        return file.read_text() if file.is_file() else None

    stale = {g.name: changed for g in gate.discover() if (changed := g.stale(text))}

    # A gate named here runs in shadow until `dispatch gates calibrate <name>` measures it again.
    assert stale == {}


#: A row of a part as the user turn renders it: the line's number, its mark, its text.
_ROW = re.compile(r'^ *(?P<number>\d+) [+ ] (?P<text>.*)$')


@dataclass
class Scripted:
    """A model that finds each line naming a node, passes it in `missed`, and finds a GPU's in `false`, by path."""

    missed: Collection[str] = ()
    false: Collection[str] = ()
    #: Words of a line it finds wherever it is shown.
    stray: str | None = None
    #: How many calls to fail, as the API does when it still fails after the SDK's retries, before answering.
    failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def ask(self, request: Request) -> Reply:
        with self._lock:
            if self.failures:
                self.failures -= 1
                return Reply(None, 'the call failed: APIConnectionError')
        found: list[dict[str, object]] = []
        path = ''
        for line in request.user.split('\n'):
            if line.startswith('## '):
                path = line.removeprefix('## ').split(' (')[0]
            elif (row := _ROW.match(line)) is not None and path:
                said = row['text']
                if (
                    ('node' in said and path not in self.missed)
                    or ('GPU' in said and path in self.false)
                    or (self.stray is not None and self.stray in said)
                ):
                    number = int(row['number'])
                    found.append({'path': path, 'line': number, 'end_line': number, 'why': f'scripted for {said}'})
        return Reply(json.dumps({'findings': found}))


def _note(n: int, *, bare: bool = False) -> str:
    """Pull request `n`'s note: a markdown file, or with `bare` a file of no extension."""
    return f'docs/notes/{n}' if bare else f'docs/notes/{n}.md'


@dataclass(frozen=True)
class History:
    """A scratch repository holding the stub gate's canon, a forge, and the pull requests of the stub gate's history."""

    repo: Path
    forge: GateMemoryForge
    cases: list[Case]


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _history(
    repo: Path, stub: Gate, size: int, *, findings: int | None = None, extra: str = '', bare: bool = False
) -> History:
    """`size` pull requests, each its own note over the last, written as the stub gate's history.

    The first `findings` of them, all of them where none is given, state where
    something runs; the rest hold the second line alone, a pass at line 1.
    """
    forge = GateMemoryForge()
    base = commit(repo, {RULE_DOCUMENT: RULE})
    cases: list[Case] = []
    for n in range(1, size + 1):
        source = f'https://github.com/Aetf/kluster/pull/{n}#issuecomment-{n}'
        if findings is None or n <= findings:
            head = commit(repo, {_note(n, bare=bare): f'It runs on node {n}.\nIt requests a GPU.\n{extra}'})
            cases += [
                Case(n, head, _note(n, bare=bare), 1, 1, Verdict.FINDING, source),
                Case(n, head, _note(n, bare=bare), 2, 2, Verdict.PASS, source),
            ]
        else:
            head = commit(repo, {_note(n, bare=bare): 'It requests a GPU.\n'})
            cases.append(Case(n, head, _note(n, bare=bare), 1, 1, Verdict.PASS, source))
        forge.requests[n] = PullRequest(n, f'docs: note {n}', '', 'MERGED', f'b-{n}', head, base, ())
        base = head
    _ = (stub.directory / CASES).write_text(''.join(f'{c.to_json()}\n' for c in cases))
    return History(repo, forge, cases)


def _calibrate(stub: Gate, history: History, model: Model) -> Calibration:
    return calibrate(stub, history.repo, lambda: model, history.forge, Git(history.repo)).record


@pytest.mark.parametrize(
    ('part', 'said'),
    [
        ('prompt', 'the prompt'),
        ('harness', 'the prompt'),
        ('counts', 'the prompt'),
        ('margin', 'the prompt'),
        ('canon', 'the canonical text'),
        ('model', 'the model'),
    ],
)
def test_a_record_whose_hash_mismatches_reads_as_shadow_and_says_why(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch, part: str, said: str
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)
    _ = _calibrate(stub, history, Scripted())
    if part == 'prompt':
        _ = (stub.directory / gate.PROMPT).write_text(f'{stub.prompt}\nAnswer briefly.\n')
    elif part == 'harness':
        monkeypatch.setattr(ask, 'FRAME', f'{ask.FRAME} Answer briefly.')
    elif part == 'counts':
        monkeypatch.setattr(ask, 'MAJORITY', ask.MAJORITY - 1)
    elif part == 'margin':
        monkeypatch.setattr(ask, 'MARGIN', ask.MARGIN + 1)
    elif part == 'canon':
        _ = (repository / RULE_DOCUMENT).write_text(RULE.replace('never where it runs', 'never where it lives'))
    else:
        stub = replace(stub, spec=replace(stub.spec, model='claude-sonnet-5-5'))

    def text(path: str) -> str | None:
        return (repository / path).read_text()

    now = stub.hashes(stub.canon(text).text)
    status, why = standing(stub.record(), now)

    record = stub.record()
    assert record is not None
    assert record.hashes.changed(now) == [said]
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


def test_a_gate_that_recovers_every_finding_and_makes_none_false_earns_blocking_on_the_ask_dispatch_gates_makes(
    tmp_path: Path, repository: Path
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, PULL_REQUESTS)

    record = _calibrate(stub, history, Scripted())

    assert (record.status, record.pull_requests, record.findings, record.recovered, record.false_findings) == (
        Status.BLOCKING,
        PULL_REQUESTS,
        PULL_REQUESTS,
        PULL_REQUESTS,
        0,
    )
    assert stub.record() == record
    # The record measures the gate as a pull request's run asks it.
    assert stub.stale(lambda path: (repository / path).read_text()) == []


#: The most review findings of `PULL_REQUESTS` a gate at the bar misses, and the most false findings it makes.
_MISSED = PULL_REQUESTS - math.ceil(RECALL * PULL_REQUESTS)
_FALSE = math.floor(FALSE_FINDINGS * PULL_REQUESTS)


@pytest.mark.parametrize(
    ('missed', 'false', 'status'),
    [
        pytest.param(_MISSED, _FALSE, Status.BLOCKING, id='at-the-bar'),
        pytest.param(_MISSED + 1, 0, Status.SHADOW, id='a-miss-past-it'),
        pytest.param(0, _FALSE + 1, Status.SHADOW, id='a-false-finding-past-it'),
    ],
)
def test_the_score_is_findings_recovered_over_the_history_and_false_findings_per_pull_request(
    tmp_path: Path, repository: Path, missed: int, false: int, status: Status
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, PULL_REQUESTS)
    scripted = Scripted(
        missed={_note(n) for n in range(1, missed + 1)},
        false={_note(n) for n in range(PULL_REQUESTS - false + 1, PULL_REQUESTS + 1)},
    )

    record = _calibrate(stub, history, scripted)

    assert (record.recovered, record.false_findings, record.status) == (
        PULL_REQUESTS - missed,
        false,
        status,
    )


@pytest.mark.parametrize(
    ('size', 'findings', 'status'),
    [
        pytest.param(PULL_REQUESTS, FINDINGS, Status.BLOCKING, id='at-the-minimum'),
        pytest.param(PULL_REQUESTS, FINDINGS - 1, Status.SHADOW, id='a-finding-short'),
        pytest.param(PULL_REQUESTS - 1, PULL_REQUESTS - 1, Status.SHADOW, id='a-pull-request-short'),
    ],
)
def test_a_history_short_of_either_minimum_earns_no_status_whatever_it_scores(
    tmp_path: Path, repository: Path, size: int, findings: int, status: Status
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, size, findings=findings)

    record = _calibrate(stub, history, Scripted())

    assert (record.recall, record.false_per_pull_request, record.status) == (1.0, 0.0, status)


def test_a_pull_request_the_gate_does_not_run_on_counts_its_findings_missed_and_itself_not_at_all(
    tmp_path: Path, repository: Path
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)
    # A change to a path outside the trigger, with a review finding tagged there.
    head = commit(repository, {'src/app.py': 'NODE = "homelab"  # It runs on the homelab node.\n'})
    held = history.forge.requests[2]
    history.forge.requests[3] = replace(held, number=3, head=head, base=held.head)
    source = 'https://github.com/Aetf/kluster/pull/3#issuecomment-3'
    extra = Case(3, head, 'src/app.py', 1, 1, Verdict.FINDING, source)
    _ = (stub.directory / CASES).write_text(''.join(f'{c.to_json()}\n' for c in [*history.cases, extra]))

    record = _calibrate(stub, history, Scripted())

    assert (record.pull_requests, record.findings, record.recovered) == (2, 3, 2)


def _entry(path: str, line: int) -> Entry:
    found = Answer(Verdict.FINDING, 'found')
    return Entry(path, line, line, (found, found, found))


def test_gate_findings_recover_review_findings_one_to_one_whatever_order_they_come_in() -> None:
    source = 'https://example.invalid/review'
    cases = [Case(1, 'a' * 40, 'a.md', line, line, Verdict.FINDING, source) for line in (10, 14)]
    # Line 12 lies within the margin of both review findings, and line 8 of the first alone.
    found = [_entry('a.md', 12), _entry('a.md', 8)]

    scores = [score(cases, found), score(cases, found[::-1])]

    assert [len(s.recovered) for s in scores] == [2, 2]
    assert [s.unconfirmed for s in scores] == [(), ()]


def test_a_gate_finding_recovers_no_review_finding_at_the_same_lines_of_another_file() -> None:
    cases = [Case(1, 'a' * 40, 'a.md', 3, 3, Verdict.FINDING, 'https://example.invalid/review')]

    scored = score(cases, [_entry('b.md', 3)])

    assert (scored.recovered, scored.unconfirmed) == ((), (_entry('b.md', 3),))


def _command(monkeypatch: pytest.MonkeyPatch, stub: Gate, history: History, scripted: Scripted) -> int:
    """`dispatch gates calibrate stub`, its gate `stub`, its checkout and forge `history`'s, its model `scripted`."""

    def named(name: str) -> Gate:
        assert name == stub.name
        return stub

    def messages() -> Model:
        return scripted

    monkeypatch.setattr(gate, 'named', named)
    monkeypatch.setattr(run, 'messages', messages)
    monkeypatch.setattr(cli, 'GateGh', lambda: history.forge)
    monkeypatch.setattr(workstation, 'repo_root', lambda: history.repo)
    return cli.main(['gates', 'calibrate', stub.name])


def test_a_run_in_which_a_sample_answered_nothing_writes_no_record_and_refuses(
    tmp_path: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)
    _ = _calibrate(stub, history, Scripted())
    before = (stub.directory / RECORD).read_text()

    # One call of six fails, so two samples still find every finding and the score would read as whole.
    assert _command(monkeypatch, stub, history, Scripted(failures=1)) == cli.REFUSED

    assert (stub.directory / RECORD).read_text() == before
    # The samples that did answer are still reported, each with its reason.
    out = capsys.readouterr().out
    assert all(f'scripted for It runs on node {n}.' in out for n in (1, 2))
    with pytest.raises(Unanswered) as refused:
        _ = calibrate(stub, repository, lambda: Scripted(failures=1), history.forge, Git(repository))
    assert refused.value.judgement.unanswered == 1


def test_a_finding_no_case_confirms_writes_no_record_and_names_its_place(tmp_path: Path, repository: Path) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2, extra=_STRAY)

    with pytest.raises(Unconfirmed) as refused:
        _ = _calibrate(stub, history, Scripted(stray='week'))

    assert not (stub.directory / RECORD).exists()
    heads = {c.pr: c.head for c in history.cases}
    assert [(r.pr, r.head, e.path, e.line) for r, e in refused.value.judgement.unconfirmed] == [
        (n, heads[n], _note(n), 6) for n in (1, 2)
    ]


def _ruled_on(history: History, refused: Unconfirmed, verdicts: Mapping[int, str]) -> int:
    """A calibration pull request whose `## Ruling` quotes the refusal whole, with the tag of `verdicts` beside its places.

    `verdicts` gives a tag by the place's line in the refusal, counting from
    0 among the lines naming one; a place it gives none is left as quoted.
    """
    calibration = 100
    held = history.forge.requests[1]
    history.forge.requests[calibration] = replace(held, number=calibration, title='gates: calibrate stub')
    lines = str(refused).split('\n')
    places = [index for index, line in enumerate(lines) if line.startswith('- #')]
    for n, index in enumerate(places):
        if n in verdicts:
            lines[index] = f'{lines[index]} {verdicts[n]}'
    _ = history.forge.say(calibration, '## Ruling\n\n' + '\n'.join(lines) + '\n')
    return calibration


#: A line, under each note's two, that the stray script finds and no case names.
_STRAY = '\n\n\nIt keeps a week of entries.\n'


def test_a_ruling_in_the_form_the_refusal_prints_confirms_each_place_it_rules_and_no_other(
    tmp_path: Path, repository: Path
) -> None:
    stub = stub_gate(tmp_path)
    # Notes with no extension, which a ruling names as any other path.
    history = _history(repository, stub, 2, extra=_STRAY, bare=True)
    with pytest.raises(Unconfirmed) as refused:
        _ = _calibrate(stub, history, Scripted(stray='week'))
    calibration = _ruled_on(history, refused.value, {0: '[gate:stub clean]'})

    _ = harvest(history.forge, HarvestGit(repository), [calibration], [stub])
    with pytest.raises(Unconfirmed) as again:
        _ = _calibrate(stub, history, Scripted(stray='week'))
    _ = harvest(history.forge, HarvestGit(repository), [_ruled_on(history, again.value, {0: '[gate:stub]'})], [stub])
    record = _calibrate(stub, history, Scripted(stray='week'))

    assert [(r.pr, e.path) for r, e in again.value.judgement.unconfirmed] == [(2, _note(2, bare=True))]
    assert (record.pull_requests, record.findings, record.recovered, record.false_findings) == (2, 3, 3, 1)


def _lost(history: History, stub: Gate, head: str) -> Case:
    """A case of pull request 3 at `head`, added to `history`'s and the stub gate's."""
    history.forge.requests[3] = replace(history.forge.requests[2], number=3, head=head)
    lost = Case(3, head, _note(3), 1, 1, Verdict.FINDING, 'https://github.com/Aetf/kluster/pull/3#issuecomment-3')
    _ = (stub.directory / CASES).write_text(''.join(f'{c.to_json()}\n' for c in [*history.cases, lost]))
    return lost


@pytest.mark.parametrize('held', [False, True], ids=['the-store-lacks-it', 'the-store-holds-it'])
def test_a_head_the_forge_no_longer_serves_drops_its_cases_whatever_the_store_holds(
    tmp_path: Path, repository: Path, held: bool
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)
    # The commit every note is written over, which the store holds and no case is at.
    gone = history.forge.requests[1].base if held else 'f' * 40
    lost = _lost(history, stub, gone)
    history.forge.unserved.add(gone)

    calibrated = calibrate(stub, repository, Scripted, history.forge, Git(repository))

    assert (calibrated.record.pull_requests, calibrated.record.findings) == (2, 2)
    assert calibrated.judgement.dropped == [(3, gone, (lost,))]


def test_a_head_the_forge_serves_and_the_store_lacks_refuses_naming_it(tmp_path: Path, repository: Path) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)
    _ = _lost(history, stub, 'f' * 40)

    with pytest.raises(PullUnreadable, match='f' * 40):
        _ = calibrate(stub, repository, Scripted, history.forge, Git(repository))


def test_a_history_every_head_of_which_the_forge_no_longer_serves_writes_no_record(
    tmp_path: Path, repository: Path
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)
    _ = _calibrate(stub, history, Scripted())
    before = (stub.directory / RECORD).read_text()
    history.forge.unserved.update(c.head for c in history.cases)

    with pytest.raises(Unmeasured) as refused:
        _ = _calibrate(stub, history, Scripted())

    assert (stub.directory / RECORD).read_text() == before
    assert [pr for pr, _, _ in refused.value.judgement.dropped] == [1, 2]


def test_calibration_reports_each_case_against_its_label_and_each_samples_reason(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = stub_gate(tmp_path)
    history = _history(repository, stub, 2)

    scripted = Scripted(missed={_note(2)}, false={_note(1)})

    assert _command(monkeypatch, stub, history, scripted) == cli.DONE

    # Each sample's reason, which the record leaves out, is printed.
    assert capsys.readouterr().out.count('scripted for It runs on node 1.') == 3
    judgement = calibrate(stub, repository, lambda: scripted, history.forge, Git(repository)).judgement
    scores = [r.score for r in judgement.reviewed]
    assert [[(c.line, e.line) for c, e in s.recovered] for s in scores] == [[(1, 1)], []]
    assert [[(e.line, c.line) for e, c in s.false] for s in scores] == [[(2, 2)], []]


@pytest.mark.parametrize(('status', 'gone'), [(422, True), (404, None)])
def test_only_a_422_from_the_commits_endpoint_reads_as_a_commit_the_forge_no_longer_serves(
    monkeypatch: pytest.MonkeyPatch, status: int, gone: bool | None
) -> None:
    def answered(_self: GateGh, args: Sequence[str], *, stdin: str | None = None) -> str:
        raise ForgeError(f'`gh {" ".join(args)}` failed: gh: Not served (HTTP {status})')

    monkeypatch.setattr(GateGh, '_run', answered)

    if gone is None:
        with pytest.raises(ForgeError):
            _ = GateGh().serves('f' * 40)
    else:
        assert GateGh().serves('f' * 40) is not gone
