"""`dispatch merge`, run with the pinned `jj` against a git remote and a forge of the cases' own.

The remote is a bare repository, and the forge's pull request reads its head
from it, so what `dispatch merge` pushes is what the forge then reports. The
forge merges as `main`'s protection does (github.md §3): only the head it was
told to expect, only a branch already up to date with `main`, and only once
every required context has reported a pass; otherwise it refuses, and reports
the merge state GitHub does (`BEHIND`, `BLOCKED`). Like the real one here, it
leaves the ledger issues open, which is the merge whose closing keywords
Aetf/kluster#934's comma list did not fire. Every wait runs on a clock of the
case's own, which only a pause advances. The records are written by
`dispatch claim` into `memory_forge`'s forge. The pair from the gates design
(Aetf/kluster-ops#917, D§5) is here: a head someone else moved, and one no
review names, against a reviewed, reported, unmoved head.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pinned_tools
import process_sessions
import pytest
from memory_forge import MemoryForge

from kluster.scripts.dispatch import audit, brief, claim, hooks, ledger, prcheck, record
from kluster.scripts.dispatch.forge import Comment, ForgeError, Milestone
from kluster.scripts.dispatch.merge import ATTEMPTS, Bucket, Check, Jj, Precondition, Refused, Stopped, Wait, merge
from kluster.scripts.dispatch.prcheck import PullRequest

#: Each command's bound: a stop-loss, which nothing asserts on.
TIMEOUT = 60

PARALLEL = 'Parallel — non-blocking'
ISSUE = 917
PULL = 4917
WORKSPACE = 'b-917'
BRANCH = 'b-917-merge'
SESSION = 'session-of-the-dispatcher'
#: Another slice of the issue, dispatched beside the one that merges.
SIBLING = 'b-917-m2'
SIBLING_BRANCH = 'b-917-m2-work'
#: Another issue the description closes, beside the dispatch's own.
OTHER = 918
#: The contexts `main`'s protection requires.
CONTEXTS = frozenset({'checks', 'changes'})
PASSED = [Check('checks', Bucket.PASS), Check('changes', Bucket.PASS)]

BODY = """{line}

## Live

Not provider-facing.

## Fails without

`test_the_work`, which reddens without it.

## Sweep

- `the work`: control: "the work". Over 2 files.

## Gate

Passed.
"""


@pytest.fixture(scope='module', autouse=True)
def pinned_jj() -> None:
    """The pinned `jj`, refused when missing rather than skipped."""
    pinned_tools.require('jj')


def _run(cwd: Path, *argv: str) -> str:
    # Through a POSIX session of its own: `jj git fetch` and `push` run git,
    # which starts processes of its own.
    return process_sessions.run(argv, cwd=cwd, text=True, check=True, timeout=TIMEOUT).stdout


@dataclass
class Remote:
    """The bare repository the forge holds, and a clone of it that is anyone else."""

    bare: Path
    other: Path

    def ref(self, name: str) -> str:
        return _run(self.bare, 'git', 'rev-parse', f'refs/heads/{name}').strip()

    def commit(self, branch: str, path: str, text: str) -> str:
        """Commit `path` onto the remote's `branch` from the other clone, as another session would."""
        _ = _run(self.other, 'git', 'fetch', '-q', 'origin')
        _ = _run(self.other, 'git', 'checkout', '-q', '-B', branch, f'origin/{branch}')
        _ = (self.other / path).write_text(text)
        _ = _run(self.other, 'git', 'add', '--', path)
        _ = _run(self.other, 'git', 'commit', '-q', '-m', f'{path} from elsewhere')
        _ = _run(self.other, 'git', 'push', '-q', '-f', 'origin', f'HEAD:{branch}')
        return self.ref(branch)


@dataclass
class Forge(MemoryForge):
    """`memory_forge`'s forge, with pull requests whose heads are the remote's branches.

    `reports` is what each read of the required checks returns in turn, the
    last for every read after it. `fail` names calls that fail, each once:
    `relabel`, `end-record` (the comment that posts one), `checks-read`, and
    `contexts`, which fails every time. `blocked` stands for a requirement
    other than the checks, which keeps every merge refused.
    """

    remote: Remote | None = None
    requests: dict[int, PullRequest] = field(default_factory=dict[int, PullRequest])
    reports: list[list[Check]] = field(default_factory=lambda: [PASSED])
    fail: set[str] = field(default_factory=set[str])
    blocked: bool = False
    #: Called once, as the first merge is asked for: another merge landing on `main` first.
    before_merge: Callable[[], None] | None = None
    closed: list[str] = field(default_factory=list[str])
    merges: int = 0
    reported: list[Check] = field(default_factory=list[Check])

    def _failing(self, call: str) -> None:
        if call in self.fail:
            self.fail.discard(call)
            raise ForgeError(f'{call} failed')

    def _behind(self, base: str, head: str) -> bool:
        assert self.remote is not None
        cmd = ['git', 'merge-base', '--is-ancestor', base, head]
        return process_sessions.run(cmd, cwd=self.remote.bare, timeout=TIMEOUT).returncode != 0

    def _blocked(self) -> bool:
        passed = {c.name for c in self.reported if c.bucket is Bucket.PASS}
        return self.blocked or not passed >= CONTEXTS

    def pull_request(self, number: int) -> PullRequest:
        held = self.requests[number]
        assert self.remote is not None
        head, base = self.remote.ref(held.branch), self.remote.ref('main')
        state = 'BEHIND' if self._behind(base, head) else 'BLOCKED' if self._blocked() else 'CLEAN'
        return replace(held, head=head, base=base, merge_state='UNKNOWN' if held.state == 'MERGED' else state)

    def required_checks(self, number: int) -> list[Check]:
        self._failing('checks-read')
        self.reported = self.reports.pop(0) if len(self.reports) > 1 else self.reports[0]
        return list(self.reported)

    def required_contexts(self) -> frozenset[str]:
        if 'contexts' in self.fail:
            raise ForgeError('contexts failed')
        return CONTEXTS

    def merge(self, number: int, head: str) -> None:
        assert self.remote is not None
        self.merges += 1
        if self.before_merge is not None:
            before, self.before_merge = self.before_merge, None
            before()
        pull = self.pull_request(number)
        if pull.head != head:
            raise ForgeError(f'head branch was modified: {pull.head}, not {head}')
        if pull.merge_state != 'CLEAN':
            raise ForgeError(f'the pull request is not mergeable: {pull.merge_state}')
        _ = _run(self.remote.bare, 'git', 'update-ref', 'refs/heads/main', head)
        self.requests[number] = replace(self.requests[number], state='MERGED')

    def comment(self, number: int, body: str) -> None:
        if body.startswith(record.HEADING) and '"event": "end"' in body:
            self._failing('end-record')
        super().comment(number, body)

    def relabel(self, number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()) -> None:
        self._failing('relabel')
        super().relabel(number, add=add, remove=remove)

    def close(self, number: int, comment: str) -> None:
        self.issues_[number].open = False
        self.closed.append(comment)


@dataclass
class Clock:
    """A clock only a pause advances."""

    now: float = 0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def wait(self) -> Wait:
        return Wait(head=60, checks=600, poll=10, sleep=self.sleep, clock=lambda: self.now)


@dataclass(frozen=True)
class FailingSecondFetch(Jj):
    """A checkout whose fetch fails on the second round, after the first round pushed."""

    fetches: list[bool] = field(default_factory=list[bool])

    def fetch(self) -> None:
        self.fetches.append(True)
        if len(self.fetches) == 2:
            raise ForgeError('the fetch failed')
        super().fetch()


@dataclass(frozen=True)
class FailingPush(Jj):
    """A checkout whose first push fails before it reaches the forge."""

    failed: list[bool] = field(default_factory=list[bool])

    def push(self, branch: str) -> None:
        if not self.failed:
            self.failed.append(True)
            raise ForgeError('the push failed')
        super().push(branch)


@dataclass
class World:
    """The remote, the dispatcher's checkout, the forge, and the head the builder reported."""

    remote: Remote
    checkout: Path
    forge: Forge
    reported: str

    def review(self, head: str) -> None:
        held = self.forge.requests[PULL]
        comment = Comment(f'## Review (combined)\n\nReviewed head: `{head}`.\n\n**Verdict: approve.**', mine=True)
        self.forge.requests[PULL] = replace(held, comments=(*held.comments, comment))

    def merge(self, remains: str | None = None, repository: Jj | None = None) -> PullRequest:
        return merge(
            self.forge,
            repository or Jj(self.checkout),
            PULL,
            self.reported,
            SESSION,
            remains=remains,
            wait=Clock().wait(),
        )

    def head(self, revision: str = BRANCH) -> str:
        return _run(self.checkout, 'jj', 'log', '-r', revision, '--no-graph', '-T', 'commit_id')

    def landed(self) -> None:
        """The work is on the forge's `main`, on top of the `main` that moved, and the dispatch has ended."""
        assert self.forge.requests[PULL].state == 'MERGED'
        main = self.remote.ref('main')
        assert _run(self.remote.bare, 'git', 'show', f'{main}:work.txt') == 'the work\n'
        assert _run(self.remote.bare, 'git', 'show', f'{main}:base.txt') == 'base.txt once main moved\n'
        assert not self.dispatches()
        assert not ledger.claimed(self.forge.issue(ISSUE))
        assert WORKSPACE not in _workspaces(self)
        assert not (self.checkout / '.claude' / 'workspaces' / WORKSPACE).exists()

    def unmerged(self) -> None:
        assert self.forge.requests[PULL].state == 'OPEN'
        assert self.dispatches()

    def dispatches(self) -> tuple[record.Dispatch, ...]:
        return record.dispatches(ISSUE, self.forge.comments(ISSUE))

    def sibling(self) -> brief.Brief:
        """Another slice of the issue claimed beside the merging one, with a workspace of its own; its brief."""
        _ = _run(self.checkout, 'jj', 'workspace', 'add', '-r', 'main', f'.claude/workspaces/{SIBLING}')
        front = f'issue: {ISSUE}\nrole: builder\ndone: Its slice lands.\nowned: [other.txt]\ncloses: false\n'
        sibling = _brief(self.checkout.parent, f'---\n{front}workspace: {SIBLING}\nbranch: {SIBLING_BRANCH}\n---\n')
        _ = claim.claim(self.forge, claim.Dispatch(sibling, SESSION), ISSUE, frozenset({'AGENTS.md'}))
        return sibling


def _brief(directory: Path, text: str) -> brief.Brief:
    path = directory / 'brief.md'
    _ = path.write_text(text)
    return brief.read(path.read_bytes())


#: A line the builder's work adds to `shared.txt`, as two changes each add one word-list entry.
ENTRY = 'shared.txt at the base\nan entry\n'


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, closes: bool = True, entry: bool = False) -> World:
    """A remote whose `main` moved after the builder pushed its branch, and a claimed, reviewable pull request.

    With `entry`, the builder's work also adds `ENTRY`'s line to `shared.txt`.
    """
    for name in [name for name in os.environ if name.startswith(('GIT_', 'JJ_'))]:
        monkeypatch.delenv(name)
    environment = {
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
        'HOME': str(tmp_path),
        'JJ_CONFIG': os.devnull,
        'JJ_USER': 'merge probe',
        'JJ_EMAIL': 'probe@example.invalid',
    }
    for role in ('AUTHOR', 'COMMITTER'):
        environment |= {f'GIT_{role}_NAME': 'merge probe', f'GIT_{role}_EMAIL': 'probe@example.invalid'}
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    bare, other, checkout = tmp_path / 'remote.git', tmp_path / 'other', tmp_path / 'checkout'
    _ = _run(tmp_path, 'git', 'init', '-q', '--bare', '-b', 'main', str(bare))
    _ = _run(tmp_path, 'git', 'clone', '-q', str(bare), str(other))
    for path in ('base.txt', 'shared.txt'):
        _ = (other / path).write_text(f'{path} at the base\n')
    _ = _run(other, 'git', 'add', '-A')
    _ = _run(other, 'git', 'commit', '-q', '-m', 'the base')
    _ = _run(other, 'git', 'push', '-q', 'origin', 'HEAD:main')
    remote = Remote(bare, other)

    # The builder's workspace, its work, and its push, as AGENTS.md has them.
    _ = _run(tmp_path, 'jj', 'git', 'clone', '--colocate', str(bare), str(checkout))
    (checkout / '.claude' / 'workspaces').mkdir(parents=True)
    _ = _run(checkout, 'jj', 'workspace', 'add', '-r', 'main', f'.claude/workspaces/{WORKSPACE}')
    workspace = checkout / '.claude' / 'workspaces' / WORKSPACE
    _ = (workspace / 'work.txt').write_text('the work\n')
    if entry:
        _ = (workspace / 'shared.txt').write_text(ENTRY)
    _ = _run(workspace, 'jj', 'describe', '-m', 'the work')
    _ = _run(workspace, 'jj', 'new')
    _ = _run(workspace, 'jj', 'bookmark', 'set', BRANCH, '-r', '@-')
    _ = _run(workspace, 'jj', 'git', 'push', '--bookmark', BRANCH)
    reported = _run(workspace, 'jj', 'log', '-r', BRANCH, '--no-graph', '-T', 'commit_id').strip()
    _ = remote.commit('main', 'base.txt', 'base.txt once main moved\n')

    forge = Forge(remote=remote)
    forge.milestones_[PARALLEL] = Milestone(PARALLEL, open=True, description='Beside the mainline.')
    forge.add(ISSUE, PARALLEL, 'area/ops', 'kind/task')
    forge.add(OTHER, PARALLEL, 'area/ops', 'kind/task')
    front = f'issue: {ISSUE}\ndone: The merge lands.\nworkspace: {WORKSPACE}\n'
    built = _brief(
        tmp_path,
        f'---\n{front}role: builder\nowned: [work.txt]\ncloses: {str(closes).lower()}\nbranch: {BRANCH}\n---\n',
    )
    _ = claim.claim(forge, claim.Dispatch(built, SESSION), ISSUE, frozenset({'AGENTS.md'}))
    line = (
        f'Closes Aetf/kluster-ops#{ISSUE}, closes Aetf/kluster-ops#{OTHER}'
        if closes
        else f'Refs Aetf/kluster-ops#{ISSUE}'
    )
    forge.requests[PULL] = PullRequest(PULL, 'dispatch: the merge', BODY.format(line=line), 'OPEN', BRANCH, '', '', ())
    reviewing = _brief(tmp_path, f'---\n{front}role: review\nowned: []\npr: {PULL}\n---\n')
    _ = claim.claim(forge, claim.Dispatch(reviewing, SESSION), ISSUE, frozenset({'AGENTS.md'}))
    return World(remote, checkout, forge, reported)


def _workspaces(world: World) -> list[str]:
    return _run(world.checkout, 'jj', 'workspace', 'list', '-T', 'name ++ "\\n"').split()


@pytest.mark.parametrize(('closes', 'remains'), [(True, None), (False, 'the live drill')], ids=['closes', 'refs'])
def test_a_reviewed_reported_unmoved_head_merges_and_its_dispatch_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closes: bool, remains: str | None
) -> None:
    world = _world(tmp_path, monkeypatch, closes=closes)
    world.review(world.reported)

    merged = world.merge(remains=remains)

    world.landed()
    # Every issue the description closes and the forge left open is closed,
    # or the dispatch's own is told what remains.
    if closes:
        assert not world.forge.issue(ISSUE).open
        assert not world.forge.issue(OTHER).open
        assert world.forge.closed == [f'Closed by {merged.url}, merged through `dispatch merge`.'] * 2
    else:
        assert world.forge.issue(ISSUE).open
        assert world.forge.issue(OTHER).open
        assert world.forge.comments(ISSUE)[-2].body.endswith(f'What remains: {remains}')


def _moved(world: World) -> None:
    _ = world.remote.commit(BRANCH, 'work.txt', 'the work, as someone else pushed it\n')
    world.review(world.reported)


def _moved_and_fetched(text: str) -> Callable[[World], None]:
    """Someone else's push of `text` over the reviewed work, which the dispatcher's routine fetch brought in."""

    def arrange(world: World) -> None:
        _ = world.remote.commit(BRANCH, 'work.txt', text)
        _ = _run(world.checkout, 'jj', 'git', 'fetch')
        world.review(world.reported)

    return arrange


def _reviewed_earlier(world: World) -> None:
    # The review read the round before the builder's latest report.
    world.review('0123456789ab' + world.reported[12:])


def _closing_beside_a_sibling(world: World) -> None:
    # The merge would close the issue another slice is still building under.
    _ = world.sibling()
    world.review(world.reported)


@pytest.mark.parametrize(
    ('arrange', 'precondition'),
    [
        (_moved, Precondition.HEAD),
        # The same change to `git patch-id`, which ignores whitespace: a
        # space taken out, and the line indented, which in Python moves a
        # statement into or out of a block.
        (_moved_and_fetched('thework\n'), Precondition.HEAD),
        (_moved_and_fetched('    the work\n'), Precondition.HEAD),
        (_reviewed_earlier, Precondition.REVIEW),
        (_closing_beside_a_sibling, Precondition.OTHERS_LIVE),
    ],
    ids=['moved-head', 'moved-head-without-a-space', 'moved-head-indented', 'unreviewed-head', 'closes-beside-another'],
)
def test_a_merge_is_refused_before_it_changes_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrange: Callable[[World], None], precondition: Precondition
) -> None:
    world = _world(tmp_path, monkeypatch)
    arrange(world)
    branch, main = world.remote.ref(BRANCH), world.remote.ref('main')
    local = _run(world.checkout, 'jj', 'log', '-r', BRANCH, '--no-graph', '-T', 'commit_id')

    with pytest.raises(Refused) as refused:
        _ = world.merge()

    assert [r.precondition for r in refused.value.refusals] == [precondition]
    assert (world.remote.ref(BRANCH), world.remote.ref('main')) == (branch, main)
    assert _run(world.checkout, 'jj', 'log', '-r', BRANCH, '--no-graph', '-T', 'commit_id') == local
    assert world.forge.requests[PULL].state == 'OPEN'
    assert world.dispatches()


def test_a_merge_ends_its_own_dispatch_and_leaves_the_issues_other_one_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aetf/kluster-ops#949: one slice of #917 merged, and the close-out ended every slice and removed its workspace."""
    world = _world(tmp_path, monkeypatch, closes=False)
    sibling = world.sibling()
    world.review(world.reported)

    _ = world.merge(remains='the other slices')

    assert [d.name.workspace for d in world.dispatches()] == [SIBLING]
    assert ledger.CLAIMED in world.forge.issue(ISSUE).labels
    assert WORKSPACE not in _workspaces(world) and SIBLING in _workspaces(world)
    assert (world.checkout / '.claude' / 'workspaces' / SIBLING).is_dir()
    # Every reader of the ledger still holds the other slice live: the claim
    # amends it, the hooks pass its spawn, report and pull request, the gates
    # find its dispatch, and the audit finds its workspace named.
    assert not claim.check(world.forge, claim.Dispatch(sibling, SESSION), ISSUE, frozenset({'AGENTS.md'})).refusals
    assert isinstance(hooks.dispatch_record(world.forge, sibling), record.Record)
    body = tmp_path / 'sibling.md'
    _ = body.write_text(BODY.format(line=f'Refs Aetf/kluster-ops#{ISSUE}'))
    opening = f'gh pr create --repo Aetf/kluster --head {SIBLING_BRANCH} --title "the slice" --body-file {body}'
    assert hooks.pr_create(hooks.Event({'command': opening}, agent_type=hooks.BUILDER), world.forge) is None
    pull = PullRequest(PULL + 1, 'the slice', body.read_text(), 'OPEN', SIBLING_BRANCH, '', '', ())
    assert prcheck.dispatched(world.forge, pull).builder.name.workspace == SIBLING
    found = audit.audit(world.forge, _workspaces(world))
    assert not [f for f in found if f.anomaly is audit.Anomaly.WORKSPACE_WITHOUT_RECORD]


def _claim_beside(world: World, workspace: str, branch: str, closes: bool) -> brief.Brief:
    """Another builder dispatch on the issue, in `workspace` on `branch`; its brief."""
    front = f'issue: {ISSUE}\nrole: builder\ndone: Its work lands.\nowned: [other.txt]\ncloses: {str(closes).lower()}\n'
    beside = _brief(world.checkout.parent, f'---\n{front}workspace: {workspace}\nbranch: {branch}\n---\n')
    _ = claim.claim(world.forge, claim.Dispatch(beside, SESSION), ISSUE, frozenset({'AGENTS.md'}))
    return beside


def test_a_merge_leaves_a_workspace_a_live_dispatch_still_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two dispatches share a workspace on different branches, and one merges."""
    world = _world(tmp_path, monkeypatch, closes=False)
    _ = _claim_beside(world, WORKSPACE, 'b-917-other', closes=False)
    world.review(world.reported)

    _ = world.merge(remains='the other branch')

    assert [d.name.branch for d in world.dispatches()] == ['b-917-other']
    assert WORKSPACE in _workspaces(world)


def test_a_merge_ends_every_dispatch_on_its_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch dispatched again in a fresh workspace after the first one's agent died: both end with its merge."""
    world = _world(tmp_path, monkeypatch)
    again = _claim_beside(world, 'b-917-again', BRANCH, closes=True)
    body = tmp_path / 'again.md'
    _ = body.write_text(BODY.format(line=f'Closes Aetf/kluster-ops#{ISSUE}'))
    opening = f'gh pr create --repo Aetf/kluster --head {BRANCH} --title "the work" --body-file {body}'
    assert hooks.pr_create(hooks.Event({'command': opening}, agent_type=hooks.BUILDER), world.forge) is None
    assert isinstance(hooks.dispatch_record(world.forge, again), record.Record)
    world.review(world.reported)

    _ = world.merge()

    world.landed()


def test_a_record_that_leaves_its_issue_open_needs_what_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, closes=False)
    world.review(world.reported)

    with pytest.raises(Refused) as refused:
        _ = world.merge()

    assert [r.precondition for r in refused.value.refusals] == [Precondition.REMAINS]


def test_a_rebase_that_conflicts_stops_with_nothing_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    _ = world.remote.commit('main', 'work.txt', 'main has a work.txt of its own\n')
    branch = world.remote.ref(BRANCH)

    with pytest.raises(Stopped, match='conflicts with main'):
        _ = world.merge()

    assert world.remote.ref(BRANCH) == branch
    world.unmerged()


def test_a_red_check_run_again_lands_on_a_second_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first run pushes its rebase and stops on the red check, so the
    # forge's head is the dispatcher's own rebase of the reported head, which
    # the review did not name. The check passes on a re-run of its job, and
    # the second run lands it.
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    world.forge.reports = [[Check('checks', Bucket.FAIL), Check('changes', Bucket.PASS)]]

    with pytest.raises(Stopped, match='checks fail'):
        _ = world.merge()
    world.unmerged()
    assert world.remote.ref(BRANCH) != world.reported

    world.forge.reports = [PASSED]
    _ = world.merge()

    world.landed()


def test_a_rebase_whose_hunk_main_already_carries_lands_on_a_second_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another pull request lands the same line on `main` first, so the
    # rebase carries one hunk fewer than the reported change; then a check
    # fails and passes on a re-run of its job.
    world = _world(tmp_path, monkeypatch, entry=True)
    world.review(world.reported)
    _ = world.remote.commit('main', 'shared.txt', ENTRY)
    world.forge.reports = [[Check('checks', Bucket.FAIL), Check('changes', Bucket.PASS)]]

    with pytest.raises(Stopped, match='checks fail'):
        _ = world.merge()
    world.forge.reports = [PASSED]
    _ = world.merge()

    world.landed()
    assert _run(world.remote.bare, 'git', 'show', f'{world.remote.ref("main")}:shared.txt') == ENTRY


def test_a_push_that_failed_is_taken_up_by_a_second_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The bookmark here is the rebase, and the forge still holds the reported head.
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)

    with pytest.raises(Stopped, match='takes it up again'):
        _ = world.merge(repository=FailingPush(world.checkout))
    assert world.remote.ref(BRANCH) == world.reported
    assert world.head() != world.reported

    _ = world.merge()

    world.landed()


@pytest.mark.parametrize(
    ('path', 'text'),
    [('later.txt', 'unreported\n'), ('work.txt', 'the\twork\n')],
    ids=['a-new-file', 'whitespace-only'],
)
def test_a_bookmark_holding_unreported_work_is_not_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, text: str
) -> None:
    # The builder's later commit, set on the bookmark here and never pushed.
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    workspace = world.checkout / '.claude' / 'workspaces' / WORKSPACE
    _ = (workspace / path).write_text(text)
    _ = _run(workspace, 'jj', 'describe', '-m', 'later')
    _ = _run(workspace, 'jj', 'new')
    _ = _run(workspace, 'jj', 'bookmark', 'set', BRANCH, '-r', '@-')

    with pytest.raises(Stopped, match='holds another change'):
        _ = world.merge()

    assert world.remote.ref(BRANCH) == world.reported
    world.unmerged()


def test_a_fold_of_the_reported_head_lands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The builder's second round is a commit of its own, reported and
    # reviewed; the dispatcher folds the two here before the merge.
    world = _world(tmp_path, monkeypatch)
    workspace = world.checkout / '.claude' / 'workspaces' / WORKSPACE
    _ = (workspace / 'work.txt').write_text('the work\n')
    _ = (workspace / 'review.txt').write_text('the fix round\n')
    _ = _run(workspace, 'jj', 'describe', '-m', 'the fix round')
    _ = _run(workspace, 'jj', 'new')
    _ = _run(workspace, 'jj', 'bookmark', 'set', BRANCH, '-r', '@-')
    _ = _run(workspace, 'jj', 'git', 'push', '--bookmark', BRANCH)
    world.reported = world.head()
    world.review(world.reported)
    _ = _run(world.checkout, 'jj', 'squash', '--from', BRANCH, '--into', f'{BRANCH}-', '-m', 'the work')
    assert world.head() != world.reported

    _ = world.merge()

    world.landed()
    main = world.remote.ref('main')
    assert _run(world.remote.bare, 'git', 'show', f'{main}:review.txt') == 'the fix round\n'
    assert _run(world.remote.bare, 'git', 'rev-list', '--count', f'{main}^..{main}') == '1\n'


def test_a_red_required_check_stops_with_nothing_merged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    world.forge.reports = [[Check('checks', Bucket.FAIL), Check('changes', Bucket.PASS)]]

    with pytest.raises(Stopped, match='checks fail'):
        _ = world.merge()

    world.unmerged()


def test_main_moving_under_the_merge_goes_round_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)

    def another_merge() -> None:
        _ = world.remote.commit('main', 'shared.txt', 'another merge\n')

    world.forge.before_merge = another_merge

    _ = world.merge()

    world.landed()
    assert world.forge.merges == 2


def test_a_forge_error_on_a_later_round_is_a_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The first round pushed, and `main` moved under its merge; the second
    # round's fetch fails before it pushes anything of its own.
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)

    def another_merge() -> None:
        _ = world.remote.commit('main', 'shared.txt', 'another merge\n')

    world.forge.before_merge = another_merge

    with pytest.raises(Stopped, match='takes it up again'):
        _ = world.merge(repository=FailingSecondFetch(world.checkout))

    world.unmerged()


def test_a_context_reported_late_is_waited_for_without_spending_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `checks` comes from a workflow whose run starts after `changes` has
    # passed, and stays unreported for as many reads as there are attempts.
    # One merge is refused on it, and the next is asked for once it reports.
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    early = [Check('changes', Bucket.PASS)]
    world.forge.reports = [early] * ATTEMPTS + [PASSED]

    _ = world.merge()

    world.landed()
    assert world.forge.merges == 2


def test_a_merge_blocked_on_anything_else_stops_naming_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    world.forge.blocked = True

    with pytest.raises(Stopped, match='its merge state is BLOCKED'):
        _ = world.merge()

    assert world.forge.merges == 1
    world.unmerged()


def test_a_failed_read_in_a_wait_is_read_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    world.forge.fail = {'checks-read'}

    _ = world.merge()

    world.landed()


def test_a_forge_error_after_the_push_is_a_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(tmp_path, monkeypatch)
    world.review(world.reported)
    world.forge.reports = [[Check('changes', Bucket.PASS)]]
    world.forge.fail = {'contexts'}

    with pytest.raises(Stopped, match='takes it up again'):
        _ = world.merge()

    world.unmerged()


@pytest.mark.parametrize(
    ('closes', 'failing'), [(True, 'relabel'), (False, 'end-record')], ids=['after-the-end', 'before-the-end']
)
def test_a_close_out_that_stopped_is_finished_by_merging_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, closes: bool, failing: str
) -> None:
    world = _world(tmp_path, monkeypatch, closes=closes)
    world.review(world.reported)
    world.forge.fail = {failing}
    remains = None if closes else 'the live drill'

    with pytest.raises(Stopped, match='close-out stopped'):
        _ = world.merge(remains=remains)
    assert world.forge.requests[PULL].state == 'MERGED'
    _ = world.merge(remains=remains)

    world.landed()
    bodies = [c.body for c in world.forge.comments(ISSUE)]
    assert sum(body.startswith(record.HEADING) and '"event": "end"' in body for body in bodies) == 1
    assert sum('What remains:' in body for body in bodies) == (0 if closes else 1)
    assert len(world.forge.closed) == (2 if closes else 0)
