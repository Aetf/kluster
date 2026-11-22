"""`dispatch file`, `claim`, `queue`, `abandon` and `audit`, against a forge of the cases' own.

Each check's pair from the gates design (Aetf/kluster-ops#917, D§5) is here: a
case it must catch, taken from what slipped, and a case it must pass. The
forge is `memory_forge`'s, which keeps issues, comments, milestones and pull
requests in memory, so a claim's record is written and read back through the
same calls `dispatch` makes of GitHub. The serialized set is read from this
checkout's AGENTS.md, the statement `dispatch` reads.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass

import pytest
from memory_forge import MemoryForge

from kluster.scripts.dispatch import audit, brief, claim, filing, ledger, milestones, record, serialized
from kluster.scripts.dispatch.claim import Precondition
from kluster.scripts.dispatch.forge import Comment, ForgeError, Milestone, Pull
from kluster.scripts.dispatch.paths import OwnedPath

M1 = 'M1 — bring-up'
M2 = 'M2 — platform'
M3 = 'M3 — workload migration'
M4 = 'M4 — legacy retirement'
PARALLEL = 'Parallel — non-blocking'
LABELS = ('area/cluster', 'kind/task')
SESSION = 'session-a'
OTHER_SESSION = 'session-b'


def _phase(forge: MemoryForge, title: str, design: int, *, closed: bool) -> None:
    """A phase whose design issue `design` is closed; formally closed, with its checkpoint, when `closed`."""
    forge.milestones_[title] = Milestone(
        title, open=not closed, description=f'Its scope.\n\nDesign: Aetf/kluster-ops#{design}\n'
    )
    forge.add(design, title, *LABELS, title=f'{title} design', open_=False)
    if closed:
        forge.add(
            design + 1,
            title,
            *LABELS,
            milestones.CHECKPOINT,
            milestones.APPROVED,
            title=f'{title[:2]} review checkpoint: audit',
            open_=False,
        )


@pytest.fixture
def forge() -> MemoryForge:
    """The phases M1–M4 open with their designs approved, and `Parallel`."""
    forge = MemoryForge()
    for title, design in ((M1, 100), (M2, 200), (M3, 300), (M4, 400)):
        _phase(forge, title, design, closed=False)
    forge.milestones_[PARALLEL] = Milestone(PARALLEL, open=True, description='Beside the mainline.')
    return forge


def _brief(issue: int, *owned: str, role: str = 'builder', **fields: object) -> brief.Brief:
    """A brief file's bytes, read the way `dispatch` reads one."""
    lines = [f'issue: {issue}', f'role: {role}', 'done: The work merges and the gate passes.']
    lines.append('owned: [' + ', '.join(f'"{path}"' for path in owned) + ']')
    if role == 'builder':
        lines.append('closes: true')
    lines += [f'{key}: {value}' for key, value in fields.items()]
    return brief.read(('---\n' + '\n'.join(lines) + '\n---\n\nThe brief.\n').encode())


def _dispatch(
    issue: int,
    *owned: str,
    role: str = 'builder',
    session: str = SESSION,
    word: str | None = None,
    **fields: object,
) -> claim.Dispatch:
    return claim.Dispatch(brief=_brief(issue, *owned, role=role, **fields), session=session, operator_word=word)


def _refused(forge: MemoryForge, dispatch: claim.Dispatch, number: int) -> set[Precondition]:
    """What refuses the claim; the forge's issues are left exactly as they were."""
    before = {n: (set(i.labels), list(i.comments)) for n, i in forge.issues_.items()}
    with pytest.raises(claim.Refused) as refused:
        _ = claim.claim(forge, dispatch, number, serialized.read())
    assert {n: (set(i.labels), list(i.comments)) for n, i in forge.issues_.items()} == before
    return {r.precondition for r in refused.value.refusals}


def _claimed(forge: MemoryForge, dispatch: claim.Dispatch, number: int) -> record.Claim:
    """Claim, and the live claim the forge then holds."""
    _ = claim.claim(forge, dispatch, number, serialized.read())
    live = ledger.claims(forge).live[number]
    assert live is not None
    return live


# -- dispatch file -------------------------------------------------------------


def test_filing_refuses_an_issue_with_no_kind_label(forge: MemoryForge) -> None:
    """ops#891–#900 and #902–#909, as first filed (D§5)."""
    before = set(forge.issues_)
    with pytest.raises(filing.FilingRefused):
        _ = filing.file(forge, title='a task', body='', labels=['area/cluster'], milestone=PARALLEL)
    assert set(forge.issues_) == before


def test_filing_refuses_a_milestone_that_is_not_open(forge: MemoryForge) -> None:
    forge.milestones_[M1] = Milestone(M1, open=False, description='')
    with pytest.raises(filing.FilingRefused):
        _ = filing.file(forge, title='a task', body='', labels=list(LABELS), milestone=M1)


def test_filing_files_an_issue_carrying_area_kind_and_a_milestone(forge: MemoryForge) -> None:
    url = filing.file(forge, title='a task', body='what to do', labels=list(LABELS), milestone=PARALLEL)
    filed = forge.issue(int(url.rsplit('/', 1)[1]))
    assert (filed.labels, filed.milestone) == (frozenset(LABELS), PARALLEL)
    assert not ledger.missing(filed.labels, filed.milestone)


# -- dispatch audit ------------------------------------------------------------


def _found(forge: MemoryForge, workspaces: Sequence[str] = ()) -> set[tuple[audit.Anomaly, str]]:
    return {(f.anomaly, f.about) for f in audit.audit(forge, workspaces)}


def test_the_audit_lists_an_open_unlabeled_issue(forge: MemoryForge) -> None:
    forge.add(1, PARALLEL, 'area/cluster')
    assert (audit.Anomaly.UNLABELED, '#1') in _found(forge)


def test_the_audit_lists_a_claim_with_no_record(forge: MemoryForge) -> None:
    forge.add(1, PARALLEL, *LABELS, ledger.CLAIMED)
    assert (audit.Anomaly.CLAIM_WITHOUT_RECORD, '#1') in _found(forge)


def test_the_audit_passes_an_alert_with_no_kind_label(forge: MemoryForge) -> None:
    forge.add(1, None, ledger.ALERT)
    assert not {about for _, about in _found(forge)} & {'#1'}


def test_the_audit_lists_a_workspace_no_live_record_names(forge: MemoryForge) -> None:
    forge.add(1, PARALLEL, *LABELS)
    _ = claim.claim(forge, _dispatch(1, 'tests/test_one.py', workspace='b-one'), 1, serialized.read())
    found = _found(forge, ['default', 'b-one', 'b-gone'])
    assert {about for anomaly, about in found if anomaly is audit.Anomaly.WORKSPACE_WITHOUT_RECORD} == {
        'workspace b-gone'
    }


# -- the milestone gate --------------------------------------------------------


def _not_formally_closed(forge: MemoryForge, how: str) -> None:
    """M1 in one of the states short of formally closed, M2 formally closed."""
    _phase(forge, M2, 200, closed=True)
    _phase(forge, M1, 100, closed=True)
    checkpoint = forge.issues_[101]
    if how == 'open':
        forge.milestones_[M1] = dataclasses.replace(forge.milestones_[M1], open=True)
    elif how == 'no checkpoint':
        checkpoint.labels.discard(milestones.CHECKPOINT)
    elif how == 'checkpoint open':
        checkpoint.open = True
    elif how == 'no acceptance':
        checkpoint.labels.discard(milestones.APPROVED)


SHORT_OF_CLOSED = ['open', 'no checkpoint', 'checkpoint open', 'no acceptance']


@pytest.mark.parametrize('how', SHORT_OF_CLOSED)
def test_a_claim_refuses_while_an_earlier_milestone_is_not_formally_closed(forge: MemoryForge, how: str) -> None:
    """An M3 issue (#903) claimed while M1 is open (#912's ruling, D§5)."""
    _not_formally_closed(forge, how)
    forge.add(903, M3, *LABELS)
    assert Precondition.MILESTONE in _refused(forge, _dispatch(903, 'tests/test_three.py'), 903)


@pytest.mark.parametrize('how', SHORT_OF_CLOSED)
def test_a_parallel_claim_passes_while_an_earlier_milestone_is_not_formally_closed(
    forge: MemoryForge, how: str
) -> None:
    """A `Parallel` issue (#911) in the same state (D§5)."""
    _not_formally_closed(forge, how)
    forge.add(911, PARALLEL, *LABELS)
    assert _claimed(forge, _dispatch(911, 'tests/test_eleven.py'), 911).live.session == SESSION


def test_a_claim_passes_once_every_earlier_milestone_is_formally_closed(forge: MemoryForge) -> None:
    _phase(forge, M1, 100, closed=True)
    _phase(forge, M2, 200, closed=True)
    forge.add(903, M3, *LABELS)
    assert _claimed(forge, _dispatch(903, 'tests/test_three.py'), 903).live.session == SESSION


def test_the_operators_word_passes_the_milestone_gate_and_is_recorded(forge: MemoryForge) -> None:
    forge.add(903, M3, *LABELS)
    word = 'Go ahead with #903 while M1 closes.'
    live = _claimed(forge, _dispatch(903, 'tests/test_three.py', word=word), 903).live
    assert live.waiver is not None
    assert live.waiver.quote == word
    assert {passed.split(':')[0] for passed in live.waiver.passed} == {M1, M2}
    assert (audit.Anomaly.OPERATOR_WORD, '#903') in _found(forge)


# -- the design-issue gate -----------------------------------------------------


def test_a_builder_claim_refuses_while_its_milestones_design_issue_is_open(forge: MemoryForge) -> None:
    """An M4 builder claim while the M4 design issue (#121) is open (D§5)."""
    for title, design in ((M1, 100), (M2, 200), (M3, 300)):
        _phase(forge, title, design, closed=True)
    forge.issues_[400].open = True
    forge.add(4001, M4, *LABELS)
    assert _refused(forge, _dispatch(4001, 'tests/test_four.py'), 4001) == {Precondition.DESIGN}


def test_a_builder_claim_refuses_where_the_milestone_names_no_design_issue(forge: MemoryForge) -> None:
    forge.milestones_[M1] = Milestone(M1, open=True, description='Its scope, and no design line.')
    forge.add(1001, M1, *LABELS)
    assert _refused(forge, _dispatch(1001, 'tests/test_one.py'), 1001) == {Precondition.DESIGN}


def test_a_design_claim_passes_on_the_open_design_issue_itself(forge: MemoryForge) -> None:
    for title, design in ((M1, 100), (M2, 200), (M3, 300)):
        _phase(forge, title, design, closed=True)
    forge.issues_[400].open = True
    live = _claimed(forge, _dispatch(400, 'docs/rfc/rfc-099-retirement.md', role='design'), 400).live
    assert live.front is not None and live.front.role is brief.Role.DESIGN


def test_an_approved_open_design_issue_passes_the_design_gate(forge: MemoryForge) -> None:
    forge.issues_[100].open = True
    forge.issues_[100].labels.add(milestones.APPROVED)
    forge.add(1001, M1, *LABELS)
    assert _claimed(forge, _dispatch(1001, 'tests/test_one.py'), 1001).live.session == SESSION


# -- path overlap --------------------------------------------------------------


def test_a_claim_overlapping_a_live_record_passes_and_records_the_overlap(forge: MemoryForge) -> None:
    """Slices 17 and 23b both owning the placement census (#911): settled at merge (§2 rule 2)."""
    forge.add(17, PARALLEL, *LABELS)
    forge.add(23, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'src/kluster/components/apps/hath/', 'tests/test_apps_placement.py'), 17)
    second = _claimed(forge, _dispatch(23, 'tests/test_apps_placement.py', session=OTHER_SESSION), 23).live
    assert len(second.overlaps) == 1 and '#17' in second.overlaps[0]


def test_two_claims_with_disjoint_paths_both_pass(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    forge.add(23, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'src/kluster/components/apps/hath/', 'docs/operations.md#§3.5'), 17)
    second = _dispatch(23, 'src/kluster/components/apps/qbittorrent/', 'docs/operations.md#§3.6', session=OTHER_SESSION)
    assert _claimed(forge, second, 23).live.overlaps == ()


def test_a_claim_records_a_path_an_open_pull_request_changes(forge: MemoryForge) -> None:
    forge.add(23, PARALLEL, *LABELS)
    forge.pulls_.append(
        Pull(number=924, body='Closes Aetf/kluster-ops#24', files=frozenset({'tests/test_apps_hath.py'}), by_bot=False)
    )
    overlaps = _claimed(forge, _dispatch(23, 'tests/test_apps_*.py'), 23).live.overlaps
    assert len(overlaps) == 1 and '#924' in overlaps[0]


def test_a_bots_pull_request_holds_no_paths(forge: MemoryForge) -> None:
    forge.add(23, PARALLEL, *LABELS)
    forge.pulls_.append(Pull(number=778, body='Update a pin.', files=frozenset({'pyproject.toml'}), by_bot=True))
    assert _claimed(forge, _dispatch(23, 'pyproject.toml'), 23).live.overlaps == ()


def test_a_claim_ends_with_abandon_and_frees_its_paths(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    forge.add(23, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_apps_placement.py'), 17)
    claim.abandon(forge, 17, SESSION, None)
    assert not forge.issues_[17].labels & set(ledger.CLAIMS)
    assert _claimed(forge, _dispatch(23, 'tests/test_apps_placement.py', session=OTHER_SESSION), 23).live.overlaps == ()


@pytest.mark.parametrize(
    ('one', 'other', 'overlap'),
    [
        ('tests/test_a.py', 'tests/test_a.py', True),
        ('tests/test_a.py', 'tests/test_b.py', False),
        ('docs/operations.md#§3.5', 'docs/operations.md#§3.6', False),
        ('docs/operations.md#§3.5', 'docs/operations.md', True),
        ('src/kluster/components/apps/', 'src/kluster/components/apps/blog/__init__.py', True),
        ('src/kluster/components/apps/blog/', 'src/kluster/components/apps/hath/', False),
        ('tests/test_dispatch_*.py', 'tests/test_dispatch_ledger.py', True),
        ('tests/test_dispatch_*.py', 'tests/test_apps_blog.py', False),
        ('tests/*.py', 'tests/live/test_drill.py', False),
        ('tests/**.py', 'tests/live/test_drill.py', True),
        ('tests/test_d*', 'tests/test_dispatch_*.py', True),
        ('tests/test_a*.py', 'tests/test_b*.py', False),
        ('src/kluster/scripts/dispatch', 'src/kluster/scripts/dispatch/claim.py', True),
        ('src/kluster/scripts/dispatch', 'src/kluster/scripts/dispatch_other.py', False),
        ('tests/*', 'tests/live', True),
        ('tests/*.py', 'tests/live', False),
        ('src/kluster/scripts/', 'src/kluster/scripts/*.py', True),
        ('src/kluster/scripts/dispatch/', 'src/kluster/scripts/*.py', False),
    ],
)
def test_two_owned_paths_overlap_exactly_when_they_can_touch_one_file(one: str, other: str, overlap: bool) -> None:
    a, b = OwnedPath.parse(one), OwnedPath.parse(other)
    assert (a.overlaps(b), b.overlaps(a)) == (overlap, overlap)


# -- the serialized files ------------------------------------------------------


def test_a_claim_refuses_a_serialized_file_an_open_pull_request_touches(forge: MemoryForge) -> None:
    """A claim owning AGENTS.md while an open pull request touches it (D§5)."""
    forge.add(23, PARALLEL, *LABELS)
    forge.pulls_.append(
        Pull(number=931, body='Closes Aetf/kluster-ops#930', files=frozenset({'AGENTS.md'}), by_bot=False)
    )
    assert Precondition.SERIALIZED in _refused(forge, _dispatch(23, 'AGENTS.md'), 23)


def test_a_claim_owning_a_serialized_file_passes_once_the_pull_request_touching_it_merged(forge: MemoryForge) -> None:
    forge.add(23, PARALLEL, *LABELS)
    assert _claimed(forge, _dispatch(23, 'AGENTS.md'), 23).live.session == SESSION


def test_a_serialized_file_serializes_claims_on_different_sections_of_it(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    forge.add(23, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'AGENTS.md#The gate'), 17)
    refused = _refused(forge, _dispatch(23, 'AGENTS.md#Writing the code', session=OTHER_SESSION), 23)
    assert refused == {Precondition.SERIALIZED}


def test_the_serialized_set_refuses_a_text_that_names_none() -> None:
    with pytest.raises(serialized.SetUnreadable):
        _ = serialized.parse('* Shared files are listed nowhere.\n')


# -- the claim itself ----------------------------------------------------------


def test_a_claim_refuses_an_issue_another_dispatcher_holds(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17)
    assert _refused(forge, _dispatch(17, 'tests/test_two.py', session=OTHER_SESSION), 17) == {Precondition.CLAIMED}


def test_a_claim_refuses_a_claim_label_with_no_record(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS, ledger.CLAIMED)
    assert _refused(forge, _dispatch(17, 'tests/test_one.py'), 17) == {Precondition.CLAIMED}


@pytest.mark.parametrize(
    ('labels', 'milestone', 'refusal'),
    [((), PARALLEL, Precondition.UNLABELED), (LABELS, None, Precondition.UNLABELED)],
)
def test_a_claim_refuses_an_issue_lacking_its_labels_or_milestone(
    forge: MemoryForge, labels: tuple[str, ...], milestone: str | None, refusal: Precondition
) -> None:
    forge.add(17, milestone, *labels)
    assert _refused(forge, _dispatch(17, 'tests/test_one.py'), 17) == {refusal}


def test_a_claim_refuses_a_brief_that_names_another_issue(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    assert _refused(forge, _dispatch(18, 'tests/test_one.py'), 17) == {Precondition.BRIEF_ISSUE}


def test_a_claim_refuses_an_rfc_slice_whose_issue_title_does_not_lead_with_it(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS, title='hath: the component')
    assert _refused(forge, _dispatch(17, 'tests/test_one.py', rfc='rfc-008 slice 17'), 17) == {Precondition.RFC_TITLE}


def test_an_rfc_slice_whose_issue_title_leads_with_it_passes(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS, title='rfc-008 slice 17: hath')
    assert _claimed(forge, _dispatch(17, 'tests/test_one.py', rfc='rfc-008 slice 17'), 17)


def test_a_review_is_recorded_under_the_build_claim_this_session_holds(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17)
    live = _claimed(forge, _dispatch(17, role='review', pr=924), 17)
    assert [r.front.role for r in live.records if r.front] == [brief.Role.BUILDER, brief.Role.REVIEW]
    assert live.owned == (OwnedPath.parse('tests/test_one.py'),)


def test_a_review_refuses_an_issue_no_claim_of_this_session_holds(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    assert _refused(forge, _dispatch(17, role='review', pr=924), 17) == {Precondition.NOT_HELD}


def test_queueing_hands_the_claim_to_the_cloud_queue_and_keeps_its_paths(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    forge.add(23, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17)
    queued = _dispatch(17, 'tests/test_one.py')
    _ = claim.queue(forge, queued, 17, serialized.read(), 'the brief, whole')
    assert ledger.QUEUED in forge.issues_[17].labels and ledger.CLAIMED not in forge.issues_[17].labels
    assert any(c.body.startswith(claim.BRIEF_HEADING) for c in forge.comments(17))
    assert _claimed(forge, _dispatch(23, 'tests/test_one.py', session=OTHER_SESSION), 23).live.overlaps


def test_abandoning_another_sessions_claim_takes_the_operators_word(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17)
    with pytest.raises(claim.Refused):
        claim.abandon(forge, 17, OTHER_SESSION, None)
    claim.abandon(forge, 17, OTHER_SESSION, 'That session died; end its claim.')
    assert ledger.claims(forge).live == {}


# -- the front matter ----------------------------------------------------------


@pytest.mark.parametrize('missing', ['issue', 'role', 'done', 'owned', 'closes'])
def test_a_builder_brief_lacking_a_field_is_refused(missing: str) -> None:
    fields = {'issue': '17', 'role': 'builder', 'done': 'It merges.', 'owned': '[tests/test_one.py]', 'closes': 'true'}
    del fields[missing]
    text = '---\n' + ''.join(f'{key}: {value}\n' for key, value in fields.items()) + '---\n'
    with pytest.raises(brief.BriefRefused):
        _ = brief.read(text.encode())


@pytest.mark.parametrize(
    ('written', 'read'),
    [
        pytest.param('done: It merges, as #931 and #938 did.', None, id='cut on its line'),
        pytest.param('done: It merges once\n  ops issue #938 lands.', None, id='cut on a wrapped line'),
        pytest.param('done:\n  It merges, see #938.', None, id='cut on a line below its key'),
        pytest.param('done: "It merges, as #931 and #938 did."', 'It merges, as #931 and #938 did.', id='quoted'),
        pytest.param(
            'done: It merges, as Aetf/kluster#931 did.', 'It merges, as Aetf/kluster#931 did.', id='a reference'
        ),
        pytest.param('done: It merges.\n# a note on the line below', 'It merges.', id='a full-line comment'),
        pytest.param('done: It merges.\n\n# a note after a blank line', 'It merges.', id='a comment after a blank'),
    ],
)
def test_a_done_a_comment_cuts_short_is_refused(written: str, read: str | None) -> None:
    """Comments on the other fields, as §1.1's example writes them, are no part of what is refused."""
    text = (
        '---\nissue: 17   # the issue\nrole: builder  # builder | design | review\n'
        f'{written}\nowned: [tests/test_one.py]\ncloses: true\n---\n'
    )
    if read is None:
        with pytest.raises(brief.BriefRefused, match='quote the value'):
            _ = brief.read(text.encode())
    else:
        assert brief.read(text.encode()).front.done == read


def test_a_brief_with_no_front_matter_is_refused() -> None:
    with pytest.raises(brief.BriefRefused):
        _ = brief.read(b'# Builder brief\n\nissue: 17\n')


# -- a run of records ----------------------------------------------------------


@dataclass
class FlakyForge(MemoryForge):
    """A forge whose next `comment` fails, the way a dropped API call does."""

    fail_next: bool = True

    def comment(self, number: int, body: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ForgeError('Post "https://api.github.com/graphql": unexpected EOF')
        super().comment(number, body)


def test_a_claim_whose_record_failed_to_post_is_retried_by_its_own_session() -> None:
    forge = FlakyForge()
    forge.milestones_[PARALLEL] = Milestone(PARALLEL, open=True, description='Beside the mainline.')
    forge.add(17, PARALLEL, *LABELS)
    with pytest.raises(ForgeError):
        _ = claim.claim(forge, _dispatch(17, 'tests/test_one.py'), 17, serialized.read())
    assert _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17).live.session == SESSION


def test_a_claim_after_its_label_came_off_by_hand_starts_a_new_run(forge: MemoryForge) -> None:
    """A merge takes `in-flight` off by hand, with no `end` record (dispatch.md §4)."""
    forge.add(17, PARALLEL, *LABELS)
    forge.add(23, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_old.py'), 17)
    forge.issues_[17].labels.discard(ledger.CLAIMED)
    again = _claimed(forge, _dispatch(17, 'tests/test_new.py', session=OTHER_SESSION), 17)
    assert again.owned == (OwnedPath.parse('tests/test_new.py'),)
    assert _claimed(forge, _dispatch(23, 'tests/test_old.py'), 23).live.overlaps == ()


def test_a_record_another_account_wrote_is_not_read(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17)
    foreign = record.Record(event=record.Event.END, session=OTHER_SESSION).render()
    forge.issues_[17].comments.append(Comment(foreign, mine=False))
    live = ledger.claims(forge).live[17]
    assert live is not None and live.owned == (OwnedPath.parse('tests/test_one.py'),)


def _plant(forge: MemoryForge, number: int, body: str) -> None:
    forge.add(number, PARALLEL, *LABELS, ledger.CLAIMED)
    forge.issues_[number].comments.append(Comment(body, mine=True))


def _block(data: dict[str, object]) -> str:
    return f'{record.HEADING}\n\n```json\n{json.dumps(data)}\n```\n'


UNREADABLE = {
    'no block': f'{record.HEADING}\n\nquoted in a review, the block left out',
    'another issue': _block(
        {'event': 'claim', 'session': SESSION, 'front': {'issue': 31, 'role': 'builder', 'owned': ['a.py']}}
    ),
    'newer version': _block({'version': record.VERSION + 1, 'event': 'claim', 'session': SESSION}),
}


@pytest.mark.parametrize('body', UNREADABLE.values(), ids=UNREADABLE.keys())
def test_an_unreadable_record_refuses_its_own_issue_and_passes_an_unrelated_claim(
    forge: MemoryForge, body: str
) -> None:
    _plant(forge, 30, body)
    forge.add(23, PARALLEL, *LABELS)
    assert _claimed(forge, _dispatch(23, 'tests/test_x.py'), 23).live.session == SESSION
    assert _refused(forge, _dispatch(30, 'tests/test_y.py'), 30) == {Precondition.RECORD_UNREADABLE}
    assert ledger.claims(forge).unreadable.keys() == {30}


def test_a_record_carrying_a_front_matter_field_this_reader_does_not_know_reads(forge: MemoryForge) -> None:
    front = {'issue': 30, 'role': 'builder', 'owned': ['tests/test_y.py'], 'a_later_field': 'x'}
    _plant(forge, 30, _block({'event': 'claim', 'session': SESSION, 'front': front}))
    live = ledger.claims(forge).live[30]
    assert live is not None and live.owned == (OwnedPath.parse('tests/test_y.py'),)


ADOPTION_WORD = 'Adopt #17: its dispatcher predates the records.'


def _adopting(issue: int, word: str | None = ADOPTION_WORD) -> claim.Dispatch:
    return claim.Dispatch(brief=_brief(issue, 'tests/test_one.py'), session=SESSION, operator_word=word, adopt=True)


def test_adopting_writes_the_record_of_a_claim_label_that_has_none_and_quotes_the_word(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS, ledger.CLAIMED)
    live = _claimed(forge, _adopting(17), 17).live
    assert live.session == SESSION
    assert live.waiver is not None and live.waiver.quote == ADOPTION_WORD
    assert _refused(forge, _adopting(17), 17) == {Precondition.CLAIMED}


def test_adopting_without_the_operators_word_refuses(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS, ledger.CLAIMED)
    assert _refused(forge, _adopting(17, word=None), 17) == {Precondition.CLAIMED}


@pytest.mark.parametrize('labels', [(), (ledger.QUEUED,)], ids=['no claim label', 'queued'])
def test_adopting_refuses_an_issue_without_in_flight(forge: MemoryForge, labels: tuple[str, ...]) -> None:
    forge.add(17, PARALLEL, *LABELS, *labels)
    assert _refused(forge, _adopting(17), 17) == {Precondition.NOT_HELD}


# -- milestone titles ----------------------------------------------------------


@pytest.mark.parametrize('title', ['M5a — later', 'm3 — lowercase', 'Phase 5'])
def test_a_claim_on_a_milestone_neither_a_phase_nor_parallel_refuses(forge: MemoryForge, title: str) -> None:
    forge.milestones_[title] = Milestone(title, open=True, description='')
    forge.add(50, title, *LABELS)
    assert _refused(forge, _dispatch(50, 'tests/test_y.py'), 50) == {Precondition.MILESTONE}


def test_a_phase_claim_refuses_while_another_milestone_cannot_be_ordered(forge: MemoryForge) -> None:
    forge.milestones_['M5a — later'] = Milestone('M5a — later', open=True, description='')
    forge.add(1001, M1, *LABELS)
    assert _refused(forge, _dispatch(1001, 'tests/test_one.py'), 1001) == {Precondition.MILESTONE}


# -- the rest of the audit -----------------------------------------------------


def test_the_audit_lists_an_issue_in_two_decision_states(forge: MemoryForge) -> None:
    forge.add(1, PARALLEL, *LABELS, 'decision/pending', 'decision/lgtm')
    assert (audit.Anomaly.TWO_DECISIONS, '#1') in _found(forge)


def test_the_audit_lists_a_claimed_build_with_no_open_pull_request(forge: MemoryForge) -> None:
    forge.add(1, PARALLEL, *LABELS)
    forge.add(2, PARALLEL, *LABELS)
    _ = claim.claim(forge, _dispatch(1, 'tests/test_one.py'), 1, serialized.read())
    _ = claim.claim(forge, _dispatch(2, 'tests/test_two.py'), 2, serialized.read())
    forge.pulls_.append(Pull(number=9, body='Closes Aetf/kluster-ops#2', files=frozenset(), by_bot=False))
    found = {about for anomaly, about in _found(forge) if anomaly is audit.Anomaly.CLAIM_WITHOUT_PULL}
    assert found == {'#1'}


def test_the_audit_lists_a_pull_request_naming_no_ledger_issue_and_passes_a_bots(forge: MemoryForge) -> None:
    forge.pulls_ += [
        Pull(number=9, body='A change.', files=frozenset(), by_bot=False),
        Pull(number=10, body='Update a pin.', files=frozenset(), by_bot=True),
    ]
    found = {about for anomaly, about in _found(forge) if anomaly is audit.Anomaly.PULL_WITHOUT_ISSUE}
    assert found == {'pull request #9'}


@pytest.mark.parametrize(
    'line',
    [
        'Closes Aetf/kluster-ops#17',
        'Closes: Aetf/kluster-ops#17',
        'fixes Aetf/kluster-ops#17',
        'Resolved: Aetf/kluster-ops#17',
        'Refs Aetf/kluster-ops#17',
    ],
)
def test_a_pull_request_names_its_issue_in_every_form_github_reads(line: str) -> None:
    assert ledger.references(f'A change.\n\n{line}\n') == {17}


def test_the_audit_names_the_issue_whose_record_does_not_read(forge: MemoryForge) -> None:
    _plant(forge, 30, UNREADABLE['no block'])
    assert (audit.Anomaly.RECORD_UNREADABLE, '#30') in _found(forge)


# -- claims whose paths cannot be read -----------------------------------------


@pytest.mark.parametrize('body', [UNREADABLE['newer version'], None], ids=['unreadable record', 'no record'])
def test_a_serialized_claim_refuses_while_a_claimed_issues_paths_cannot_be_read(
    forge: MemoryForge, body: str | None
) -> None:
    """P3b and P3c of the second review: the unread claim may hold the serialized file."""
    if body is None:
        forge.add(30, PARALLEL, *LABELS, ledger.CLAIMED)
    else:
        _plant(forge, 30, body)
    forge.add(23, PARALLEL, *LABELS)
    with pytest.raises(claim.Refused) as refused:
        _ = claim.claim(forge, _dispatch(23, 'AGENTS.md'), 23, serialized.read())
    assert [r.precondition for r in refused.value.refusals] == [Precondition.SERIALIZED]
    assert '#30' in refused.value.refusals[0].detail
    assert _claimed(forge, _dispatch(23, 'tests/test_x.py'), 23).live.overlaps


def test_parallel_work_passes_while_a_milestone_cannot_be_ordered(forge: MemoryForge) -> None:
    forge.milestones_['M5a — later'] = Milestone('M5a — later', open=True, description='')
    forge.add(911, PARALLEL, *LABELS)
    assert _claimed(forge, _dispatch(911, 'tests/test_eleven.py'), 911).live.session == SESSION


@pytest.mark.parametrize('body', UNREADABLE.values(), ids=UNREADABLE.keys())
def test_an_unreadable_records_refusal_names_its_issue(forge: MemoryForge, body: str) -> None:
    _plant(forge, 30, body)
    with pytest.raises(claim.Refused) as refused:
        _ = claim.claim(forge, _dispatch(30, 'tests/test_y.py'), 30, serialized.read())
    assert all('#30' in r.detail for r in refused.value.refusals)


QUOTE = 'Go ahead.\nOn both counts.'
#: A `done` sentence that carries a block of its own, ahead of the record's.
FORGED = 'It merges.\n```json\n{"event": "end", "session": "forged"}\n```'


def test_a_record_reads_its_own_block_and_quotes_every_line_of_the_word() -> None:
    front = dataclasses.replace(_brief(17, 'tests/test_one.py').front, done=FORGED)
    written = record.Record(
        event=record.Event.CLAIM,
        session=SESSION,
        front=record.Front.of(front),
        waiver=record.Waiver(quote=QUOTE, passed=('M1 — bring-up: the milestone is open',)),
    ).render()
    assert '\n```json\n{"event": "end"' in written, 'the forged block stands where a block is read'
    read = record.parse(17, Comment(written, mine=True))
    assert read is not None and (read.event, read.session) == (record.Event.CLAIM, SESSION)
    quoted = [line for line in written.splitlines() if line.startswith('> ')]
    assert [line.removeprefix('> ') for line in quoted] == QUOTE.splitlines()


# -- the operator's word, and line endings ---------------------------------------


@pytest.mark.parametrize('word', ['', '   ', '\n\t'], ids=['empty', 'spaces', 'whitespace'])
def test_a_blank_operators_word_is_refused_wherever_it_is_taken(forge: MemoryForge, word: str) -> None:
    """An unset shell variable, `--operator-word "$WORD"`, would pass a gate and leave no quote."""
    forge.add(17, PARALLEL, *LABELS, ledger.CLAIMED)
    with pytest.raises(claim.WordRefused):
        _ = _dispatch(903, 'tests/test_three.py', word=word)
    with pytest.raises(claim.WordRefused):
        _ = _adopting(17, word=word)
    with pytest.raises(claim.WordRefused):
        claim.abandon(forge, 17, SESSION, word)
    assert ledger.CLAIMED in forge.issues_[17].labels and not forge.issues_[17].comments


def test_an_issue_owning_a_serialized_file_adopts_its_own_unrecorded_claim(forge: MemoryForge) -> None:
    """#917's position: its hand claim is the one unknown, and it is its own."""
    forge.add(917, PARALLEL, *LABELS, ledger.CLAIMED)
    adopting = claim.Dispatch(
        brief=_brief(917, 'AGENTS.md', 'pyproject.toml'), session=SESSION, operator_word=ADOPTION_WORD, adopt=True
    )
    assert _claimed(forge, adopting, 917).live.session == SESSION


def test_a_record_saved_with_crlf_line_endings_reads(forge: MemoryForge) -> None:
    forge.add(17, PARALLEL, *LABELS)
    _ = _claimed(forge, _dispatch(17, 'tests/test_one.py'), 17)
    (written,) = forge.issues_[17].comments
    forge.issues_[17].comments[0] = Comment(written.body.replace('\n', '\r\n'), mine=True)
    live = ledger.claims(forge).live[17]
    assert live is not None and live.owned == (OwnedPath.parse('tests/test_one.py'),)
