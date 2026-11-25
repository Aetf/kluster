"""The model-gate harness: a stub gate run end to end against recorded replies, the comment it posts, and appeals.

The stub gate (`gate_stub`) asks about three lines a pull request writes into
a note. Its recorded replies split on two of them and leave one unanswered
in one sample, so the majority, the evidence of every sample and the
disagreement all show. The client is the SDK's own, given a transport that
serves those replies, so no case here reaches the network or holds a key.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from gate_stub import (
    PULL,
    RULE,
    RULE_DOCUMENT,
    SPEC,
    STUB,
    GateMemoryForge,
    commit,
    pull_request,
    recorded,
    scratch_repository,
    stub_gate,
)

from kluster.scripts.dispatch.gates import ask, comment, run
from kluster.scripts.dispatch.gates.appeal import AppealRefused, appeals, apply, record
from kluster.scripts.dispatch.gates.calibration import Status
from kluster.scripts.dispatch.gates.extract import Change, Item, change, message_path
from kluster.scripts.dispatch.gates.gate import RECORD, Gate
from kluster.scripts.dispatch.gates.model import KeyMissing, Model, Reply
from kluster.scripts.dispatch.gates.prose import blocks
from kluster.scripts.dispatch.gates.reading import markdown
from kluster.scripts.dispatch.gates.verdict import Answer, Verdict
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
    held = gate.hashes(gate.canon(lambda _: RULE).text)
    _ = (gate.directory / RECORD).write_text(
        json.dumps(
            {
                'hashes': {'prompt': held.prompt, 'canon': held.canon, 'model': held.model},
                'cases': 'unread here',
                'positives': 8,
                'negatives': 8,
                'recall': 1.0,
                'false_positives': 0.0,
                'disagreement': 0.0,
                'pair': True,
                'status': 'blocking',
            }
        )
    )


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

    # One batch, asked three times, with the canon as the head holds it and the items as data.
    assert len(replies.requests) == 3
    request = replies.requests[0]
    assert request['model'] == gate.spec.model
    assert request['output_config']['format'] == {'type': 'json_schema', 'schema': ask.SCHEMA}
    assert 'never where it runs' in request['system']
    assert all(line in request['messages'][0]['content'] for line in LINES)
    # The one comment, headed by the head, and recording every item with its three samples.
    (posted_comment,) = forge.pull_comments(PULL)
    head = forge.requests[PULL].head
    assert posted_comment.body.startswith(f'## Gate findings (head {head})')
    assert posted is not None
    parsed = comment.parse(posted_comment.body)
    assert parsed is not None
    assert _verdicts(parsed) == _verdicts(posted.evidence)
    (ran,) = posted.evidence.gates
    assert [e.finding for e in ran.entries] == [True, False, False]
    assert [sorted(s.verdict if s else 'none' for s in e.samples) for e in ran.entries] == [
        ['finding', 'finding', 'pass'],
        ['finding', 'pass', 'pass'],
        ['none', 'pass', 'pass'],
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

    again = run.review(forge, Git(repository), PULL, [gate], recorded().model)

    assert again is not None
    assert again.edited
    (held,) = forge.pull_comments(PULL)
    evidence = comment.parse(held.body)
    assert evidence is not None
    assert [e.appeal for e in evidence.gates[0].entries] == ['a reason.', None, None]


def test_an_appeal_naming_no_standing_finding_is_refused_and_nothing_is_written(
    tmp_path: Path, repository: Path
) -> None:
    forge = _forge(repository)
    _ = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], recorded().model)
    before = forge.pull_comments(PULL)

    with pytest.raises(AppealRefused) as refused:
        _ = record(forge, PULL, f'Gate appeal: stub {NOTE}:1: fine.\nGate appeal: stub {NOTE}:2: also fine.')

    assert refused.value.refusals == [
        f'Gate appeal: stub {NOTE}:2: stub has no finding standing there, nor an item left undecided, '
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


def test_a_change_reads_both_sides_of_each_hunk_the_tree_at_its_head_and_its_description(repository: Path) -> None:
    kept = 'One.\nTwo.\nThree.\nFour.\n'
    gone = 'A note.\nAnother.\n'
    base = commit(repository, {'docs/kept.md': kept, 'docs/gone.md': gone})
    _ = (repository / 'docs' / 'gone.md').unlink()
    # Line 2 rewritten as two, and line 4 deleted: a hunk that only deletes reaches the change too.
    head = commit(repository, {'docs/kept.md': 'One.\n2.\n2b.\nThree.\n', 'docs/new.md': 'New.\n'})

    changed = change(Git(repository), base, head, 'the description')

    files = {f.path: f for f in changed.files}
    # Each hunk with both of its sides, as `git diff -U0` gives them: a side that holds no line is a count of 0.
    assert files['docs/kept.md'].hunks == (Hunk(2, 1, 2, 2), Hunk(4, 1, 4, 0))
    assert files['docs/kept.md'].written == ((2, 3),)
    assert (files['docs/gone.md'].hunks, files['docs/gone.md'].written) == ((Hunk(1, 2, 0, 0),), ())
    assert (files['docs/new.md'].hunks, files['docs/new.md'].written) == ((Hunk(0, 0, 1, 1),), ((1, 1),))
    assert (changed.base_text('docs/gone.md'), changed.text('docs/gone.md')) == (gone, None)
    assert sorted(changed.tree()) == ['docs/kept.md', 'docs/new.md']
    assert changed.body == 'the description'


def test_items_are_cut_into_batches_at_the_cap_and_one_over_it_goes_alone() -> None:
    small = [Item('a.md', n, n, 'x' * 30) for n in range(1, 4)]
    large = Item('a.md', 9, 9, 'y' * 3000)
    cap = 3 * ask.estimate(small[0])

    cut = ask.batches([*small, large, small[0]], cap)

    assert cut == [small, [large], [small[0]]]


def test_a_reply_that_answers_an_item_twice_or_by_an_unknown_id_answers_nothing_for_it() -> None:
    reply = Reply(
        json.dumps(
            {
                'items': [
                    {'id': 0, 'verdict': 'finding', 'why': 'one'},
                    {'id': 0, 'verdict': 'pass', 'why': 'two'},
                    {'id': 1, 'verdict': 'pass', 'why': 'three'},
                    {'id': 7, 'verdict': 'finding', 'why': 'nobody'},
                ]
            }
        )
    )

    assert ask.answers(reply, 3) == [None, Answer(Verdict.PASS, 'three'), None]


def test_an_appeal_line_is_read_wherever_the_report_holds_it() -> None:
    report = 'Report.\n  Gate appeal: change-detector tests/test_x.py:12: it holds a relation.\nThanks.'

    assert [(a.gate, a.path, a.line, a.reason) for a in appeals(report)] == [
        ('change-detector', 'tests/test_x.py', 12, 'it holds a relation.')
    ]


def test_a_finding_in_a_commits_message_is_named_by_its_place_and_appealed_there() -> None:
    sha = 'c' * 40
    found = comment.Entry(message_path(sha), 3, 5, (Answer(Verdict.FINDING, 'it narrates'),) * 3)
    ran = comment.Ran('narration', 'claude-haiku-5-5', Status.BLOCKING, 'why', '', (found,))
    evidence = comment.Evidence('a' * 40, (ran,))

    rendered = comment.render(evidence)
    assert f'`:commit/{sha}:3–5`' in rendered
    held = comment.parse(rendered)
    assert held is not None
    appealed = apply(held, appeals(f'Gate appeal: narration :commit/{sha}:4: it states what the tree holds.'))
    assert appealed.unanswered == []
    # A commit named by less than its full SHA is refused, saying so, rather than read as no appeal at all.
    with pytest.raises(AppealRefused, match='full SHA'):
        _ = apply(held, appeals(f'Gate appeal: narration :commit/{sha[:12]}:4: it states what the tree holds.'))


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


def test_a_gate_whose_extractor_raises_asks_nothing_names_why_and_leaves_the_others_to_run(
    tmp_path: Path, repository: Path
) -> None:
    def broken(change: Change) -> list[Item]:
        _ = change.text(NOTE)
        raise SyntaxError('invalid syntax')

    copy = tmp_path / 'broken'
    _ = shutil.copytree(STUB, copy)
    failing = Gate.load(copy, replace(SPEC, extract=broken))
    _blocking(failing)

    posted = run.review(_forge(repository), Git(repository), PULL, [stub_gate(tmp_path), failing], recorded().model)

    assert posted is not None
    ran = {r.gate: r for r in posted.evidence.gates}
    assert (ran['broken'].asked, ran['broken'].status) == (False, Status.BLOCKING)
    assert f'after reading {NOTE}: SyntaxError' in ran['broken'].why
    assert ran['stub'].asked
    assert [e.line for e in ran['stub'].entries] == [1, 2, 3]
    assert run.refuses(0, posted)


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


def test_a_deterministic_finding_refuses_whatever_the_gates_say() -> None:
    assert run.refuses(1, None)
    assert not run.refuses(0, None)


def test_two_thousand_items_fit_in_one_comment_and_read_back_to_the_same_verdicts() -> None:
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


def test_a_run_too_large_to_record_asks_nothing_and_posts_why(tmp_path: Path, repository: Path) -> None:
    base = commit(repository, {RULE_DOCUMENT: RULE})
    head = commit(repository, {NOTE: ''.join(f'Line {n} of a long note.\n' for n in range(9000))})
    forge = GateMemoryForge()
    pull_request(forge, base, head)

    def no_model() -> Model:
        raise AssertionError('a run that cannot be recorded asks nothing')

    posted = run.review(forge, Git(repository), PULL, [stub_gate(tmp_path)], no_model)

    assert posted is not None
    (ran,) = posted.evidence.gates
    assert (ran.asked, ran.entries) == (False, ())
    assert 'its 9000 items would not fit in one comment' in forge.pull_comments(PULL)[0].body
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


def test_an_item_a_blocking_gate_left_undecided_holds_until_appealed() -> None:
    undecided = comment.Entry('a.md', 1, 1, (Answer(Verdict.FINDING, 'one'), None, Answer(Verdict.PASS, 'two')))
    passed = comment.Entry('a.md', 2, 2, (Answer(Verdict.PASS, 'three'), None, Answer(Verdict.PASS, 'four')))

    def posted(status: Status) -> run.Posted:
        ran = comment.Ran('stub', 'claude-haiku-5-5', status, 'why', '', (undecided, passed))
        return run.Posted(comment.Evidence('a' * 40, (ran,)), edited=False)

    assert (run.refuses(0, posted(Status.BLOCKING)), run.refuses(0, posted(Status.SHADOW))) == (True, False)
    assert 'no verdict: too few of its samples answered' in comment.render(posted(Status.BLOCKING).evidence)
    appealed = apply(posted(Status.BLOCKING).evidence, appeals('Gate appeal: stub a.md:1: a reason.'))
    assert not run.refuses(0, run.Posted(appealed, edited=True))


def test_a_split_item_reads_back_with_its_reasons_and_a_unanimous_pass_with_none() -> None:
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


@pytest.mark.parametrize(
    ('path', 'text', 'passages'),
    [
        # A `#!` line names an interpreter; the comment under it is prose.
        pytest.param(
            'scripts/run.sh',
            '#!/bin/sh\n# Runs once a day.\nexec true\n',
            [(2, 2, 'Runs once a day.')],
            id='interpreter',
        ),
        # A comment in a shell fence is the command's, and the paragraph after the fence is prose.
        pytest.param(
            'docs/run.md', '```sh\n# held by hand\nmake\n```\nThen it runs.\n', [(5, 5, 'Then it runs.')], id='fence'
        ),
        # A fence closes only on a run as long as its own, so an example fence inside it is code too.
        pytest.param(
            'docs/run.md',
            'Para.\n\n````markdown\n```python\nx = 1\n```\nInside still.\n````\n\nAfter.\n',
            [(1, 1, 'Para.'), (10, 10, 'After.')],
            id='long-fence',
        ),
        # Front matter is data for whatever reads the file.
        pytest.param(
            'docs/agent.md', '---\nname: builder\ndescription: builds\n---\n\nBody.\n', [(6, 6, 'Body.')], id='front'
        ),
        # A comment after code is its own passage, apart from the comment alone on the next line, and a
        # docstring's text ends where its string does.
        pytest.param(
            'src/run.py',
            'x = 1  # one\n# Two lines\n# of prose.\n"""A doc."""  # after\n',
            [(1, 1, 'one'), (2, 3, 'Two lines of prose.'), (4, 4, 'A doc.'), (4, 4, 'after')],
            id='after-code',
        ),
        # The same outside Python, where the marker has whitespace on both sides.
        pytest.param(
            'conf/a.yaml',
            'key: 1  # one\n# own\nv: "${#a}"\nurl: http://host/x#y\n',
            [(1, 1, 'one'), (2, 2, 'own')],
            id='after-code-yaml',
        ),
        # A line holding only its marker ends a paragraph of comments.
        pytest.param('renovate.json5', '// One.\n//\n// Two.\n', [(1, 1, 'One.'), (3, 3, 'Two.')], id='paragraph'),
        # A template's Jinja comment is prose whatever it renders, and no other reading takes a line of it.
        pytest.param(
            'conf/a.yaml.j2',
            '{#\n# Kept here.\n#}\n# Read.\nkey: 1\n',
            [(1, 3, ' # Kept here. '), (4, 4, 'Read.')],
            id='jinja',
        ),
        pytest.param(
            'docs/a.md.j2', '{#\nKept here.\n#}\nBody.\n', [(1, 3, ' Kept here. '), (4, 4, 'Body.')], id='jinja-md'
        ),
    ],
)
def test_the_prose_reader_draws_each_passage_by_the_files_language(
    path: str, text: str, passages: list[tuple[int, int, str]]
) -> None:
    assert sorted((b.first, b.last, b.text) for b in blocks(path, text)) == passages


def test_the_brief_gates_reader_takes_what_is_code_in_markdown_from_the_prose_reader() -> None:
    # A `~~~` fence holding a backtick example, then an indented block: code for every gate that reads markdown.
    text = 'Lead.\n\n~~~\n```sh\nmake\n```\nstill code\n~~~\n\nAfter.\n\n    indented code\n'
    read = [(p.first, p.last, p.prose) for p in markdown(text)]

    assert read == [(1, 1, True), (3, 8, False), (10, 10, True), (12, 12, False)]
    assert [(b.first, b.last) for b in sorted(blocks('docs/a.md', text), key=lambda b: b.first)] == [(1, 1), (10, 10)]
