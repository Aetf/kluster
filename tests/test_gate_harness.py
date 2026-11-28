"""The model-gate harness: a stub gate run end to end against recorded replies, the comment it posts, and appeals.

The stub gate (`gate_stub`) is shown a note a pull request writes, whole.
Its recorded replies find the first line in two samples of three, the
third in one, and the second in none, so the majority, the evidence of
every sample and the places no sample found all show. The client is the
SDK's own, given a transport that serves those replies, so no case here
reaches the network or holds a key. What a gate is shown of a change, how a
long one is cut into calls, which lines a finding may stand on, how the
samples' findings are matched into places, and which of its cases a gate's
prompt carries as examples are cased on their own beside it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field, replace
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from gate_stub import (
    PULL,
    RATE_LIMITED,
    RULE,
    RULE_DOCUMENT,
    GateMemoryForge,
    Recorded,
    commit,
    pull_request,
    recorded,
    reply,
    scratch_repository,
    stub_gate,
)

from kluster.scripts.dispatch.gates import ask, comment, examples, model, run, view
from kluster.scripts.dispatch.gates.appeal import AppealRefused, appeals, apply, record
from kluster.scripts.dispatch.gates.ask import Finding
from kluster.scripts.dispatch.gates.calibration import FINDINGS, MARGIN, PULL_REQUESTS, Calibration, Status, near
from kluster.scripts.dispatch.gates.cases import Case
from kluster.scripts.dispatch.gates.change import change, message_path
from kluster.scripts.dispatch.gates.gate import CASES, RECORD, Gate
from kluster.scripts.dispatch.gates.model import KeyMissing, Model, Reply, Request
from kluster.scripts.dispatch.gates.verdict import SAMPLES, Answer, Verdict
from kluster.scripts.dispatch.prcheck import Git, Hunk

NOTE = 'docs/notes/placement.md'
LINES = ['The cache runs on the homelab node.', 'The cache requests a GPU.', 'The cache keeps a week of entries.']


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _forge(repository: Path) -> GateMemoryForge:
    """A forge whose pull request writes `LINES` into `NOTE` over a base that holds the rule."""
    base = commit(repository, {RULE_DOCUMENT: RULE, 'README.md': 'A repository.\n'})
    head = commit(repository, {NOTE: '\n'.join(LINES) + '\n'})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    return forge


def _blocking(gate: Gate) -> None:
    """Write the record of a calibration that earned `blocking` for `gate` as the scratch tree holds it."""
    canon = gate.canon(lambda _: RULE).text
    measured = Calibration(
        gate.hashes(canon),
        gate.measured(canon),
        'unread here',
        pull_requests=PULL_REQUESTS,
        findings=FINDINGS,
        recovered=FINDINGS,
        false_findings=0,
    )
    _ = (gate.directory / RECORD).write_text(measured.to_json())


def _verdicts(evidence: comment.Evidence) -> list[tuple[str, str, int, int, tuple[Verdict | None, ...]]]:
    return [
        (ran.gate, e.path, e.line, e.end_line, tuple(s.verdict if s else None for s in e.samples))
        for ran in evidence.gates
        for e in ran.entries
    ]


def test_a_stub_gate_runs_end_to_end_against_recorded_replies(tmp_path: Path, repository: Path) -> None:
    forge = _forge(repository)
    gate = stub_gate(tmp_path)
    replies = recorded()

    posted = run.review(forge, Git(repository), PULL, [gate], replies.model)

    # One call, asked three times, with the canon as the head holds it and the note whole as data; the file
    # outside the gate's trigger is not shown.
    assert len(replies.requests) == 3
    request = replies.requests[0]
    assert request['model'] == gate.spec.model
    schema = request['output_config']['format']['schema']
    # The answer can name only a path the call showed.
    assert schema['properties']['findings']['items']['properties']['path']['enum'] == [NOTE]
    assert 'never where it runs' in request['system']
    content = request['messages'][0]['content']
    assert all(line in content for line in LINES)
    assert 'A repository.' not in content
    # The one comment, headed by the head, recording each place a sample found with the word of all three.
    (posted_comment,) = forge.pull_comments(PULL)
    head = forge.requests[PULL].head
    assert posted_comment.body.startswith(f'## Gate findings (head {head})')
    assert posted is not None
    parsed = comment.parse(posted_comment.body)
    assert parsed is not None
    assert _verdicts(parsed) == _verdicts(posted.evidence)
    (ran,) = posted.evidence.gates
    assert [(e.line, e.finding) for e in ran.entries] == [(1, True), (3, False)]
    assert [sorted(s.verdict for s in e.samples if s) for e in ran.entries] == [
        ['finding', 'finding', 'pass'],
        ['finding', 'pass', 'pass'],
    ]
    assert ran.rule.endswith(f'/blob/{head}/{RULE_DOCUMENT}#L3-L6')


def test_a_gate_without_a_record_runs_in_shadow_and_its_finding_blocks_nothing(
    tmp_path: Path, repository: Path
) -> None:
    forge = _forge(repository)

    posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], recorded().model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert (ran.status, ran.findings != []) == (Status.SHADOW, True)
    assert 'no calibration record' in ran.why
    assert not run.refuses(0, posted)


def test_a_blocking_finding_stands_unanswered_until_its_appeal_is_recorded(tmp_path: Path, repository: Path) -> None:
    forge = _forge(repository)
    gate = stub_gate(tmp_path)
    _blocking(gate)
    posted = run.review(forge, Git(repository), PULL, [gate], recorded().model)
    assert posted is not None
    assert [(ran.gate, e.place) for ran, e in posted.evidence.unanswered] == [('stub', f'{NOTE}:1')]
    assert run.refuses(0, posted)

    appealed = record(forge, PULL, f'Done.\n\nGate appeal: stub {NOTE}:1: the node is a fact the host declares.\n')

    assert appealed.unanswered == []
    assert not run.refuses(0, run.Posted(appealed, edited=True))
    (held,) = forge.pull_comments(PULL)
    parsed = comment.parse(held.body)
    assert parsed is not None
    assert parsed.unanswered == []
    assert 'Appealed: `the node is a fact the host declares.`' in held.body


def test_a_run_at_the_same_head_edits_its_comment_and_keeps_the_appeals(tmp_path: Path, repository: Path) -> None:
    forge = _forge(repository)
    gate = stub_gate(tmp_path)
    _ = run.review(forge, Git(repository), PULL, [gate], recorded().model)
    _ = record(forge, PULL, f'Gate appeal: stub {NOTE}:1: a reason.')
    # The samples of the run again name the place a line wider than the appealed one.
    moved = Recorded(
        [
            reply([{'path': NOTE, 'line': 1, 'end_line': 2, 'why': 'It states where the service runs.'}]),
            reply([{'path': NOTE, 'line': 2, 'end_line': 2, 'why': 'It names the node.'}]),
            reply([]),
        ]
    )

    again = run.review(forge, Git(repository), PULL, [gate], moved.model)

    assert again is not None
    assert again.edited
    (held,) = forge.pull_comments(PULL)
    evidence = comment.parse(held.body)
    assert evidence is not None
    assert [(e.line, e.end_line, e.appeal) for e in evidence.gates[0].entries] == [(1, 2, 'a reason.')]


def test_an_appeal_naming_no_standing_finding_is_refused_and_nothing_is_written(
    tmp_path: Path, repository: Path
) -> None:
    forge = _forge(repository)
    _ = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], recorded().model)
    before = forge.pull_comments(PULL)

    with pytest.raises(AppealRefused) as refused:
        _ = record(forge, PULL, f'Gate appeal: stub {NOTE}:1: fine.\nGate appeal: stub {NOTE}:2: also fine.')

    assert refused.value.refusals == [
        f'Gate appeal: stub {NOTE}:2: stub has no finding standing there, nor a place left undecided, '
        f'at {forge.requests[PULL].head[:12]}'
    ]
    assert forge.pull_comments(PULL) == before


def test_a_change_no_gate_is_triggered_by_asks_nothing_and_posts_nothing(tmp_path: Path, repository: Path) -> None:
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {'src/app.py': 'print("hello")\n'})
    forge = GateMemoryForge()
    pull_request(forge, base, head)

    def no_model() -> Model:
        raise AssertionError('nothing is asked, so no key is read')

    assert run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], no_model) is None
    assert forge.pull_comments(PULL) == []


def test_a_change_reads_both_sides_of_each_hunk(repository: Path) -> None:
    kept = 'One.\nTwo.\nThree.\nFour.\n'
    gone = 'A note.\nAnother.\n'
    base = commit(repository, {'docs/kept.md': kept, 'docs/gone.md': gone})
    _ = (repository / 'docs' / 'gone.md').unlink()
    # Line 2 rewritten as two, and line 4 deleted: a hunk that only deletes reaches the change too.
    head = commit(repository, {'docs/kept.md': 'One.\n2.\n2b.\nThree.\n', 'docs/new.md': 'New.\n'})

    changed = change(Git(repository), base, head)

    files = {f.path: f for f in changed.files}
    # Each hunk with both of its sides, as `git diff -U0` gives them: a side that holds no line is a count of 0.
    assert files['docs/kept.md'].hunks == (Hunk(2, 1, 2, 2), Hunk(4, 1, 4, 0))
    assert files['docs/kept.md'].written == ((2, 3),)
    assert (files['docs/gone.md'].hunks, files['docs/gone.md'].written) == ((Hunk(1, 2, 0, 0),), ())
    assert (files['docs/new.md'].hunks, files['docs/new.md'].written) == ((Hunk(0, 0, 1, 1),), ((1, 1),))
    assert (changed.base_text('docs/gone.md'), changed.text('docs/gone.md')) == (gone, None)


def test_an_appeal_line_is_read_wherever_the_report_holds_it() -> None:
    report = 'Report.\n  Gate appeal: two-words tests/test_x.py:12: it holds a relation.\nThanks.'

    assert [(a.gate, a.path, a.line, a.reason) for a in appeals(report)] == [
        ('two-words', 'tests/test_x.py', 12, 'it holds a relation.')
    ]


def _entry(path: str, line: int, end_line: int, letters: str, appeal: str | None = None) -> comment.Entry:
    said = {'f': Answer(Verdict.FINDING, 'a reason'), 'p': Answer(Verdict.PASS, ''), '-': None}
    return comment.Entry(path, line, end_line, tuple(said[letter] for letter in letters), appeal)


def _run(*gates: tuple[str, tuple[comment.Entry, ...]]) -> comment.Evidence:
    ran = tuple(comment.Ran(gate, 'claude-haiku-5-5', Status.BLOCKING, 'why', '', entries) for gate, entries in gates)
    return comment.Evidence('a' * 40, ran)


def test_an_appeal_of_an_undecided_place_answers_no_finding_the_run_again_makes_there() -> None:
    held = _run(('stub', (_entry('a.md', 1, 200, '--p', appeal='the call failed'),)))
    again = _run(('stub', (_entry('a.md', 40, 41, 'ffp'), _entry('a.md', 150, 151, 'ffp'))))
    same = _run(('stub', (_entry('a.md', 1, 200, '--p'),)))

    assert [e.place for _, e in again.appealed(held).unanswered] == ['a.md:40–41', 'a.md:150–151']
    # The same call failing again at the same head leaves the same undecided place, which the appeal answers.
    assert same.appealed(held).unanswered == []


def test_an_appeal_of_a_finding_answers_one_finding_the_run_again_makes_near_it() -> None:
    held = _run(('stub', (_entry('a.md', 10, 12, 'ffp', appeal='a reason'),)))
    again = _run(('stub', (_entry('a.md', 10, 11, 'ffp'), _entry('a.md', 13, 14, 'fpf'))))

    assert [e.place for _, e in again.appealed(held).unanswered] == ['a.md:13–14']


@pytest.mark.parametrize(
    ('gate', 'path'),
    [pytest.param('other', 'a.md', id='another gate'), pytest.param('stub', 'b.md', id='another path')],
)
def test_an_appeal_answers_nothing_of_another_gate_or_path_at_the_same_lines(gate: str, path: str) -> None:
    held = _run(('stub', (_entry('a.md', 10, 12, 'ffp', appeal='a reason'),)), ('other', ()))
    again = _run(*((name, (_entry(path, 10, 12, 'ffp'),) if name == gate else ()) for name in ('stub', 'other')))

    assert [(r.gate, e.place) for r, e in again.appealed(held).unanswered] == [(gate, f'{path}:10–12')]


def test_a_finding_in_a_commits_message_is_named_by_its_place_and_appealed_there() -> None:
    sha = 'c' * 40
    found = comment.Entry(message_path(sha), 3, 5, (Answer(Verdict.FINDING, 'a reason'),) * 3)
    ran = comment.Ran('stub', 'claude-haiku-5-5', Status.BLOCKING, 'why', '', (found,))
    evidence = comment.Evidence('a' * 40, (ran,))

    rendered = comment.render(evidence)
    assert f'`:commit/{sha}:3–5`' in rendered
    held = comment.parse(rendered)
    assert held is not None
    appealed = apply(held, appeals(f'Gate appeal: stub :commit/{sha}:4: it holds to the rule.'))
    assert appealed.unanswered == []
    # A commit named by less than its full SHA is refused, saying so, rather than read as no appeal at all.
    with pytest.raises(AppealRefused, match='full SHA'):
        _ = apply(held, appeals(f'Gate appeal: stub :commit/{sha[:12]}:4: it holds to the rule.'))


def test_a_planted_findings_comment_by_another_account_is_neither_edited_nor_read(
    tmp_path: Path, repository: Path
) -> None:
    forge = _forge(repository)
    head = forge.requests[PULL].head
    planted = forge.say(PULL, f'## Gate findings (head {head})\n\nNothing found.\n', mine=False)

    posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], recorded().model)

    assert posted is not None
    assert not posted.edited
    assert forge.pull_comments(PULL)[0] == planted
    assert run.findings_comment(forge.pull_comments(PULL), head) == forge.pull_comments(PULL)[1]


def test_a_gate_no_model_can_ask_still_posts_and_refuses_only_where_it_blocks(tmp_path: Path, repository: Path) -> None:
    def no_key() -> Model:
        raise KeyMissing('no model gates key on this machine')

    forge = _forge(repository)
    shadow = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path / 'shadow')], no_key)
    blocking_gate = stub_gate(tmp_path / 'blocking')
    _blocking(blocking_gate)
    blocking = run.review(_forge_again(forge), Git(repository), PULL, [blocking_gate], no_key)

    assert shadow is not None
    assert blocking is not None
    (ran,) = shadow.evidence.gates
    assert (ran.asked, ran.entries) == (False, ())
    assert 'no model gates key on this machine' in forge.pull_comments(PULL)[0].body
    assert (run.refuses(0, shadow), run.refuses(0, blocking)) == (False, True)


def _forge_again(forge: GateMemoryForge) -> GateMemoryForge:
    """A forge holding the same pull request as `forge`, and none of its comments."""
    again = GateMemoryForge()
    again.requests = dict(forge.requests)
    return again


def test_a_call_the_api_fails_is_a_sample_that_answered_nothing_and_the_comment_is_posted(
    tmp_path: Path, repository: Path
) -> None:
    forge = _forge(repository)
    replies = recorded()
    replies.status = 529

    posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], replies.model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert ran.silent
    assert 'No sample answered any of them.' in forge.pull_comments(PULL)[0].body
    assert not run.refuses(0, posted)


def test_a_call_the_api_rate_limits_waits_as_it_says_and_its_sample_answers(
    tmp_path: Path, repository: Path, caplog: pytest.LogCaptureFixture
) -> None:
    forge = _forge(repository)
    replies = recorded()
    replies.limited = 1

    with caplog.at_level(logging.INFO, logger=model.__name__):
        posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], replies.model)

    # The refused call is made again after the wait the API named, and every sample answers.
    assert (len(replies.requests), replies.slept) == (SAMPLES + 1, [20.0])
    assert posted is not None
    (ran,) = posted.evidence.gates
    assert all(s is not None for e in ran.entries for s in e.samples)
    assert [(e.line, e.finding) for e in ran.entries] == [(1, True), (3, False)]
    # The wait is logged before it is slept, with the API's reason and its length.
    (waited,) = [r.getMessage() for r in caplog.records if 'rate-limited' in r.getMessage()]
    assert 'waiting 20 s' in waited
    assert RATE_LIMITED['error']['message'] in waited


def test_a_call_the_api_rate_limits_past_the_bound_is_a_sample_that_answered_nothing(
    tmp_path: Path, repository: Path, caplog: pytest.LogCaptureFixture
) -> None:
    forge = _forge(repository)
    replies = recorded()
    replies.limited = 10 * SAMPLES
    half = model.RATE_LIMIT_WAIT / 2
    replies.retry_after = str(half)

    with caplog.at_level(logging.INFO, logger=model.__name__):
        posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], replies.model)

    # Each call waits until its waits reach the bound, and its sample then answers nothing.
    assert (len(replies.requests), replies.slept) == (3 * SAMPLES, [half] * 2 * SAMPLES)
    assert posted is not None
    (ran,) = posted.evidence.gates
    assert ran.silent
    past = [r for r in caplog.records if r.levelno == logging.WARNING and 'past the' in r.getMessage()]
    assert len(past) == SAMPLES


def test_a_deterministic_finding_refuses_whatever_the_gates_say() -> None:
    assert run.refuses(1, None)
    assert not run.refuses(0, None)


def test_two_thousand_places_fit_in_one_comment_and_read_back_to_the_same_verdicts() -> None:
    reason = 'The line states where the service runs, which the scheduler derives from what it declares.'
    entries = tuple(
        comment.Entry(
            f'src/kluster/components/apps/a_long_application_name_{n % 40:02}/__init__.py',
            n,
            n,
            tuple(Answer(Verdict.FINDING if (n + k) % 7 == 0 else Verdict.PASS, reason) for k in range(3)),
        )
        for n in range(1, 2001)
    )
    evidence = comment.Evidence('a' * 40, (comment.Ran('stub', 'claude-haiku-5-5', Status.SHADOW, 'why', '', entries),))

    body = comment.render(evidence)

    assert len(body) <= comment.LIMIT
    parsed = comment.parse(body)
    assert parsed is not None
    assert sorted(_verdicts(parsed)) == sorted(_verdicts(evidence))


@dataclass
class EveryLine:
    """A model that finds every line each call shows, and counts its calls."""

    calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def ask(self, request: Request) -> Reply:
        with self._lock:
            self.calls += 1
        shown = re.findall(r'^ *(\d+) \+ ', request.user, re.M)
        found = [{'path': NOTE, 'line': int(n), 'end_line': int(n), 'why': 'f', 'breaks_rule': True} for n in shown]
        return Reply(json.dumps({'findings': found}))


def test_a_run_whose_answers_are_too_many_to_record_is_recorded_as_giving_no_verdict(
    tmp_path: Path, repository: Path
) -> None:
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {NOTE: ''.join(f'Line {n} of a long note.\n' for n in range(1, 9001))})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    every = EveryLine()

    posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], lambda: every)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert (ran.asked, ran.dropped, ran.entries) == (False, True, ())
    assert every.calls > 0
    body = forge.pull_comments(PULL)[0].body
    # It asked, and says so: what it answered was dropped.
    assert 'gave no verdict, its answers dropped: its 9000 places would not fit in one comment' in body
    assert 'asked nothing' not in body
    assert not run.refuses(0, posted)


def test_a_blocking_gate_whose_canon_does_not_read_at_the_head_stays_blocking_and_refuses(
    tmp_path: Path, repository: Path
) -> None:
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {RULE_DOCUMENT: '# Rules\n\n## Another\n\nElse.\n', NOTE: 'A line.\n'})
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    gate = stub_gate(tmp_path)
    _blocking(gate)

    posted = run.review(forge, Git(repository), PULL, [gate], recorded().model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert (ran.status, ran.asked) == (Status.BLOCKING, False)
    assert run.refuses(0, posted)


def test_a_place_a_blocking_gate_left_undecided_holds_until_appealed() -> None:
    undecided = comment.Entry('a.md', 1, 1, (Answer(Verdict.FINDING, 'one'), None, Answer(Verdict.PASS, 'two')))
    passed = comment.Entry('a.md', 2, 2, (Answer(Verdict.PASS, 'three'), None, Answer(Verdict.PASS, 'four')))

    def posted(status: Status) -> run.Posted:
        ran = comment.Ran('stub', 'claude-haiku-5-5', status, 'why', '', (undecided, passed))
        return run.Posted(comment.Evidence('a' * 40, (ran,)), edited=False)

    assert (run.refuses(0, posted(Status.BLOCKING)), run.refuses(0, posted(Status.SHADOW))) == (True, False)
    assert 'no verdict: too few of its samples answered' in comment.render(posted(Status.BLOCKING).evidence)
    appealed = apply(posted(Status.BLOCKING).evidence, appeals('Gate appeal: stub a.md:1: a reason.'))
    assert not run.refuses(0, run.Posted(appealed, edited=True))


def test_a_split_place_reads_back_with_its_reasons_and_a_unanimous_pass_with_none() -> None:
    split = comment.Entry('a.md', 1, 1, (Answer(Verdict.FINDING, 'one'), Answer(Verdict.PASS, 'two'), None))
    unanimous = comment.Entry('a.md', 2, 2, tuple(Answer(Verdict.PASS, 'fine') for _ in range(3)))
    evidence = comment.Evidence(
        'a' * 40, (comment.Ran('stub', 'claude-haiku-5-5', Status.SHADOW, 'why', '', (split, unanimous)),)
    )

    parsed = comment.parse(comment.render(evidence))

    assert parsed is not None
    assert parsed.gates[0].entries == (
        split,
        comment.Entry('a.md', 2, 2, tuple(Answer(Verdict.PASS, '') for _ in range(3))),
    )


def test_a_reason_is_rendered_on_one_line_where_no_name_in_it_notifies() -> None:
    assert comment.one_line('Ask @someone\nabout it.') == '`Ask @someone about it.`'
    assert comment.one_line('a `span` here') == '`` a `span` here ``'


def _part(path: str, total: int) -> view.Part:
    """A part showing every line of a file of `total` lines, none of them written."""
    return view.Part(path, 'modified', total, tuple(view.Row(n, view.KEPT, f'line {n}') for n in range(1, total + 1)))


def test_a_gate_is_shown_each_file_its_trigger_matches_whole_with_the_diff_inline(repository: Path) -> None:
    base = commit(
        repository,
        {
            'docs/a.md': 'Zero.\nOne.\nTwo.\nThree.\nMid.\nFour.\nEnd.\n',
            'docs/gone.md': 'Gone.\n',
            'src/app.py': 'x = 1\n',
        },
    )
    _ = (repository / 'docs' / 'gone.md').unlink()
    head = commit(
        repository,
        {
            'docs/a.md': 'One.\nDeux.\nThree.\nFour.\n',
            'docs/blob.md': 'a\0b\n',
            'src/app.py': 'x = 2\n',
            'tests/data/held.md': 'Recorded.\n',
        },
        'docs: a note\n\nIts body.\n',
    )
    changed = change(Git(repository), base, head)

    (shown,) = view.parts(changed, ('docs/**',))

    # A deleted file and one that is no text are not shown. A removed line stands where it was, unnumbered:
    # at the top, ahead of what replaces it, among lines the change leaves, and at the end.
    assert (shown.path, shown.kind, shown.total) == ('docs/a.md', 'modified', 4)
    assert [(r.number, r.mark, r.text) for r in shown.rows] == [
        (None, '-', 'Zero.'),
        (1, ' ', 'One.'),
        (None, '-', 'Two.'),
        (2, '+', 'Deux.'),
        (3, ' ', 'Three.'),
        (None, '-', 'Mid.'),
        (4, ' ', 'Four.'),
        (None, '-', 'End.'),
    ]
    # A trigger of every path takes each file this repository writes and the message of each commit, at the place
    # that names it.
    every = view.parts(changed, ('**',))
    assert [(p.path, p.kind, p.total) for p in every] == [
        ('docs/a.md', 'modified', 4),
        ('src/app.py', 'modified', 1),
        (message_path(head), 'commit message', 3),
    ]


def test_a_part_past_a_calls_bound_is_cut_into_runs_and_the_runs_into_calls() -> None:
    rows = [view.Row(n, view.KEPT, f'line {n}') for n in range(1, 41)]
    # A removed line ahead of line 21: it travels with that line, whichever run it falls in.
    rows.insert(20, view.Row(None, view.REMOVED, 'x'))
    long = view.Part('docs/long.md', 'modified', 40, tuple(rows))
    short = _part('docs/short.md', 2)
    cap = long.size() // 3

    cut = ask.calls([long, short], cap)

    runs = [part for call in cut for part in call]
    assert all(sum(p.size() for p in call) <= cap for call in cut)
    assert [row.number for run in runs[:-1] for row in run.rows] == [row.number for row in rows]
    assert runs[-1] == short
    assert all(f'lines {r.first}–{r.last}' in r.render() for r in runs[:-1])
    # At every bound, no run but the last ends on the removed line.
    assert all(run.rows[-1].number is not None for bound in range(20, long.size()) for run in long.runs(bound))


def _twelve(name: str) -> str:
    return ''.join(f'{name} line {n}.\n' for n in range(1, 13))


def _writes(repository: Path) -> tuple[str, list[view.Part]]:
    """The head of a change, and what a gate whose trigger takes every path is shown of it.

    The change rewrites line 6 of one file, deletes line 6 of another and
    the last line of a third, adds a fourth, and its one commit's message
    runs to twelve lines.
    """
    base = commit(
        repository,
        {
            'docs/modified.md': _twelve('Modified'),
            'docs/removed.md': _twelve('Removed'),
            'docs/trailing.md': _twelve('Trailing'),
        },
    )
    removed = _twelve('Removed').splitlines(keepends=True)
    head = commit(
        repository,
        {
            'docs/modified.md': _twelve('Modified').replace('Modified line 6.', 'Line 6, rewritten.'),
            'docs/removed.md': ''.join(removed[:5] + removed[6:]),
            'docs/trailing.md': ''.join(_twelve('Trailing').splitlines(keepends=True)[:11]),
            'docs/added.md': _twelve('Added'),
        },
        'docs: notes\n\n' + ''.join(f'Body line {n}.\n' for n in range(3, 13)),
    )
    return head, view.parts(change(Git(repository), base, head), ('**',))


def _asked(parts: list[view.Part], replies: Recorded) -> ask.Judged:
    (judged,) = ask.judge([ask.Asked('You are a stub.', 'claude-haiku-5-5', parts)], replies.model)
    return judged


@pytest.mark.parametrize(
    ('path', 'kept', 'dropped'),
    [
        # Line 6 rewritten: the margin either side of it, and nothing beyond.
        pytest.param('docs/modified.md', [6 - MARGIN, 6 + MARGIN], [6 - MARGIN - 1, 6 + MARGIN + 1], id='modified'),
        # Old line 6 removed, from between lines 5 and 6 at the head: either side of where it stood.
        pytest.param('docs/removed.md', [5 - MARGIN, 6 + MARGIN], [5 - MARGIN - 1, 6 + MARGIN + 1], id='deletion'),
        # The last line removed, after line 11, the last at the head: only the line ahead of it is left to name.
        pytest.param('docs/trailing.md', [11 - MARGIN, 11], [11 - MARGIN - 1], id='trailing deletion'),
        pytest.param('docs/added.md', [1, 12], [], id='added'),
        pytest.param(':commit', [1, 12], [], id='commit message'),
    ],
)
def test_a_finding_stands_only_within_the_margin_of_a_line_the_change_writes(
    repository: Path, path: str, kept: list[int], dropped: list[int]
) -> None:
    head, parts = _writes(repository)
    path = message_path(head) if path == ':commit' else path
    said: list[dict[str, object]] = [
        {'path': path, 'line': n, 'end_line': n, 'why': f'at {n}'} for n in sorted(kept + dropped)
    ]

    judged = _asked(parts, Recorded([reply(said)]))

    # Every sample found every line asked, so each line kept is a finding of its own on every sample.
    assert [(e.path, e.line, e.end_line, e.finding) for e in judged.entries] == [(path, n, n, True) for n in kept]
    assert judged.dropped == ({path: SAMPLES * len(dropped)} if dropped else {})


def test_findings_on_lines_the_change_does_not_write_are_dropped_before_the_samples_are_matched(
    repository: Path,
) -> None:
    _, parts = _writes(repository)

    def at(line: int) -> dict[str, Any]:
        return reply([{'path': 'docs/modified.md', 'line': line, 'end_line': line, 'why': f'at {line}'}])

    # Two samples agree only on a line past the margin of the rewritten line 6; the third finds a line within it,
    # within the margin of theirs, which would join them into one place standing on all three.
    judged = _asked(parts, Recorded([at(6 + MARGIN + 1), at(6 + MARGIN + 1), at(6 + MARGIN - 1)]))

    assert [(e.line, e.end_line, e.finding) for e in judged.entries] == [(6 + MARGIN - 1, 6 + MARGIN - 1, False)]


def test_an_item_its_sample_judged_clean_is_set_aside_and_one_it_judged_to_break_the_rule_stands(
    repository: Path,
) -> None:
    _, parts = _writes(repository)
    said: list[dict[str, object]] = [
        {'path': 'docs/modified.md', 'line': 6, 'end_line': 6, 'why': 'Not a count; no finding.', 'breaks_rule': False},
        {'path': 'docs/added.md', 'line': 3, 'end_line': 3, 'why': 'It counts the members.', 'breaks_rule': True},
    ]

    judged = _asked(parts, Recorded([reply(said)]))

    assert [(e.path, e.line, e.end_line, e.finding) for e in judged.entries] == [('docs/added.md', 3, 3, True)]
    # The one call's samples each set the clean item aside.
    assert judged.clean == (SAMPLES,)


def test_two_samples_that_agree_only_on_a_place_they_judged_clean_make_no_place(repository: Path) -> None:
    _, parts = _writes(repository)

    def at(breaks: bool) -> dict[str, Any]:
        return reply([{'path': 'docs/modified.md', 'line': 6, 'end_line': 6, 'why': 'at 6', 'breaks_rule': breaks}])

    judged = _asked(parts, Recorded([at(False), at(False), reply([])]))

    assert judged.entries == []


def test_a_finding_at_a_line_the_call_did_not_show_is_left_out_and_one_running_past_it_is_held_to_it() -> None:
    call = [_part('docs/a.md', 3)]
    said = [
        {'path': 'docs/a.md', 'line': 2, 'end_line': 9, 'why': 'runs past', 'breaks_rule': True},
        {'path': 'docs/a.md', 'line': 7, 'end_line': 7, 'why': 'not shown', 'breaks_rule': True},
    ]
    stray = [*said, {'path': 'src/x.py', 'line': 1, 'end_line': 1, 'why': 'no such part', 'breaks_rule': True}]

    found = ask.findings(Reply(json.dumps({'findings': said})), call)

    assert found.found == [Finding('docs/a.md', 2, 3, 'runs past')]
    # A path the call did not show fails the schema, so the sample answered nothing rather than passed.
    assert ask.findings(Reply(json.dumps({'findings': stray})), call).found is None


def test_samples_findings_within_the_margin_name_one_place_and_a_finding_stands_on_two() -> None:
    def at(line: int, end_line: int | None = None) -> Finding:
        return Finding('docs/a.md', line, end_line or line, f'at {line}')

    far = 10 + MARGIN + 1
    samples: list[ask.Found] = [[at(10), at(30)], [at(11, 12), at(10 + far)], [at(10)]]

    entries = ask.matched([_part('docs/a.md', 40)], samples)

    letters = {(e.line, e.end_line): ''.join(s.verdict.value[0] if s else '-' for s in e.samples) for e in entries}
    assert letters == {(10, 12): 'fff', (10 + far, 10 + far): 'pfp', (30, 30): 'fpp'}
    assert [(e.line, e.finding) for e in sorted(entries, key=lambda e: e.line)] == [
        (10, True),
        (10 + far, False),
        (30, False),
    ]


@pytest.mark.parametrize(
    'lines',
    [
        pytest.param([(8, 8), (10, 10), (11, 11)], id='a chain within the margin'),
        pytest.param([(10, 30), (12, 12), (31, 31)], id='one past the end of a long finding'),
    ],
)
def test_the_places_are_the_same_whatever_order_the_samples_come_in(lines: list[tuple[int, int]]) -> None:
    def places(samples: list[ask.Found]) -> set[tuple[int, int, str]]:
        entries = ask.matched([_part('docs/a.md', 40)], samples)
        return {(e.line, e.end_line, ''.join(sorted(s.verdict.value[0] for s in e.samples if s))) for e in entries}

    found = [[Finding('docs/a.md', line, end_line, 'why')] for line, end_line in lines]
    first, last = lines[0][0], lines[-1][1]

    assert {frozenset(places(list(order))) for order in permutations(found)} == {frozenset({(first, last, 'fff')})}


@pytest.mark.parametrize(
    ('samples', 'expected'),
    [
        # 6 is near 2–4 and nothing of the place 3 opens, which holds the second finding of its sample alone.
        pytest.param([[(6, 6)], [(2, 2), (3, 3)], [(2, 4)]], {(2, 6, 'fff'), (3, 3, 'f')}, id='after a cut'),
        # 31 is near 10–30, which already holds 12 of the sample that also names 13.
        pytest.param([[(10, 30)], [(12, 12), (13, 13)], [(31, 31)]], {(10, 31, 'fff'), (13, 13, 'f')}, id='long'),
    ],
)
def test_a_place_is_linked_within_itself_each_finding_near_another_of_it(
    samples: list[list[tuple[int, int]]], expected: set[tuple[int, int, str]]
) -> None:
    found = [[Finding('docs/a.md', line, end_line, 'why') for line, end_line in sample] for sample in samples]

    for order in permutations(found):
        held = ask.places([(n, f) for n, sample in enumerate(order) for f in sample], len(order))
        named = [[f for f in place if f is not None] for place in held]
        assert {(min(f.line for f in fs), max(f.end_line for f in fs), 'f' * len(fs)) for fs in named} == expected
        for fs in named:
            assert len(fs) == 1 or all(
                any(near(f.line, f.end_line, g.line, g.end_line) for g in fs if g is not f) for f in fs
            )


def test_where_unanswered_samples_could_still_make_a_finding_stand_each_part_of_the_call_is_undecided() -> None:
    call = [_part('docs/a.md', 3), _part('docs/b.md', 5)]

    entries = ask.matched(call, [None, None, [Finding('docs/a.md', 2, 2, 'one')]])
    one_missing = ask.matched(call, [None, [], []])

    assert [(e.path, e.line, e.end_line, e.undecided) for e in entries] == [
        ('docs/a.md', 2, 2, True),
        ('docs/a.md', 1, 3, True),
        ('docs/b.md', 1, 5, True),
    ]
    # One sample short leaves the two that answered a majority to pass on.
    assert one_missing == []


def _case(path: str, verdict: Verdict, written: str) -> Case:
    return Case(1, 'a' * 40, path, 1, 1, verdict, f'https://example.invalid/{path}', written)


def test_the_examples_share_the_most_directories_then_are_the_latest_a_finding_and_a_pass_in_turn() -> None:
    finding = Verdict.FINDING
    near_old = _case('docs/notes/x.md', finding, '2026-11-01T00:00:00Z')
    near_new = _case('docs/notes/z.md', finding, '2026-11-02T00:00:00Z')
    far_newest = _case('docs/other/y.md', finding, '2026-11-03T00:00:00Z')
    near_pass = _case('docs/notes/w.md', Verdict.PASS, '2026-10-01T00:00:00Z')
    far_pass = _case('src/q.py', Verdict.PASS, '2026-11-05T00:00:00Z')
    cases = [far_pass, near_old, far_newest, near_pass, near_new]
    seen = ['docs/notes/placement.md']

    chosen = examples.select(cases, seen, lambda case: case, 4)
    # A case the store cannot show is passed over for the next of its kind.
    shown = examples.select(cases, seen, lambda case: None if case is near_new else case, 4)

    assert chosen == [near_new, near_pass, near_old, far_pass]
    assert shown == [near_old, near_pass, far_newest, far_pass]


def test_a_pull_requests_run_carries_examples_with_their_lines_and_the_reason_their_tag_binds(
    tmp_path: Path, repository: Path
) -> None:
    _ = commit(repository, {RULE_DOCUMENT: RULE})
    # A change reviewed before the one under review, which its base holds.
    earlier = commit(repository, {'docs/notes/queue.md': 'The queue runs on the edge node.\nThe queue keeps a day.\n'})
    head = commit(repository, {NOTE: '\n'.join(LINES) + '\n'})
    forge = GateMemoryForge()
    pull_request(forge, earlier, head)
    review = forge.say(
        4000,
        f'## Review (head {earlier})\n\n'
        '- docs/notes/queue.md:1 names the node the queue runs on [gate:stub]\n'
        '- docs/notes/queue.md:2 [gate:stub clean] states a retention, which the rule allows\n\n'
        'A paragraph that names no place.\n',
    )
    gate = stub_gate(tmp_path)
    held = [
        Case(4000, earlier, 'docs/notes/queue.md', 1, 1, Verdict.FINDING, review.url, review.created),
        Case(4000, earlier, 'docs/notes/queue.md', 2, 2, Verdict.PASS, review.url, review.created),
    ]
    _ = (gate.directory / CASES).write_text(''.join(f'{c.to_json()}\n' for c in held))
    replies = recorded()

    _ = run.review(forge, Git(repository), PULL, [gate], replies.model)

    system = replies.requests[0]['system']
    assert system.startswith('You are shown a change under review.')
    shown = system[system.index(examples.HEADING) :]
    assert 'The queue runs on the edge node.' in shown
    assert 'The queue keeps a day.' in shown
    assert 'The review: docs/notes/queue.md:1 names the node the queue runs on\n' in shown
    assert 'The review: docs/notes/queue.md:2 states a retention, which the rule allows' in shown
    assert 'names no place' not in shown
    assert '[gate:' not in shown


def test_a_comment_that_tags_nothing_gives_a_finding_the_unit_naming_its_place_and_a_pass_nothing() -> None:
    body = (
        '## Review (head 0123456789ab)\n\n'
        '**1. should-fix: `docs/notes/queue.md:3–5`, a placement.** It names the node the queue runs on.\n\n'
        'The rest of `docs/notes/queue.md:40` reads well.\n'
    )
    finding = Case(1, 'a' * 40, 'docs/notes/queue.md', 4, 4, Verdict.FINDING, 'https://example.invalid/review')
    held = replace(finding, verdict=Verdict.PASS)

    assert examples.cut(body, 'stub', finding) == (
        '**1. should-fix: `docs/notes/queue.md:3–5`, a placement.** It names the node the queue runs on.'
    )
    assert examples.cut(body, 'stub', held) is None
    # A comment that tags anything gives a case only the unit its tag binds.
    assert examples.cut(f'{body}\n- docs/notes/other.md:1 [gate:stub]\n', 'stub', finding) is None


#: Two review comments of this repository, recorded as the forge serves them, which name places with no tag.
REVIEWS = Path(__file__).parent / 'data' / 'gates' / 'reviews'


@pytest.mark.parametrize(
    ('comment', 'gate', 'case', 'reason'),
    [
        pytest.param(
            '7033524978',
            'stub',
            Case(896, 'a1ed77e19f2e', 'src/kluster/components/apps/jellyfin/__init__.py', 61, 82, Verdict.FINDING, ''),
            '**4. `src/kluster/components/apps/jellyfin/__init__.py:61` — should-fix.** The owned set is a list',
            id='its-first-line-over-an-earlier-unit-inside-it',
        ),
        pytest.param(
            '7065069765',
            'count',
            Case(927, 'a107653d79a6', 'src/kluster/components/monitoring/__init__.py', 164, 164, Verdict.FINDING, ''),
            '`src/kluster/components/monitoring/__init__.py:164`: "the chart\'s four and the history store".',
            id='its-exact-place-over-an-earlier-unit-opening-on-it',
        ),
    ],
)
def test_a_comment_that_tags_nothing_gives_a_finding_the_unit_naming_the_closest_place(
    comment: str, gate: str, case: Case, reason: str
) -> None:
    body = (REVIEWS / f'{comment}.txt').read_text()

    found = examples.cut(body, gate, case)

    assert found is not None
    assert found.startswith(reason)


def test_two_units_naming_places_as_close_to_a_finding_give_it_no_reason() -> None:
    body = (
        '## Review (head 0123456789ab)\n\n'
        '- `docs/notes/queue.md:4` names the node the queue runs on.\n'
        '- `docs/notes/queue.md:4` repeats the count of its replicas.\n'
    )
    finding = Case(1, 'a' * 40, 'docs/notes/queue.md', 4, 4, Verdict.FINDING, 'https://example.invalid/review')

    assert examples.cut(body, 'stub', finding) is None
