"""`dispatch-hook`: each hook, fed the event Claude Code writes, against a forge and a repository of the cases' own.

The event is the documented one: the shape of the hooks reference's
`PreToolUse` example, with `tool_input` as its "PreToolUse input" section
gives it for `Bash` (`command`, `description`, `timeout`,
`run_in_background`) and for `Agent` (`prompt`, `description`,
`subagent_type`, `model`), a `SubagentHandback` call's report as
`tool_input.message`, and `agent_id` and `agent_type` inside a subagent
("Common input fields"). That is https://code.claude.com/docs/en/hooks.md as
it reads for Claude Code 2.1.319. Every case runs `main`, so the event is read
from standard input and the decision is read back from standard output and
the exit status, the way Claude Code reads them.

Each hook's pair from the gates design (Aetf/kluster-ops#917, D§5) is here: a
case it must catch, taken from what slipped, and a case it must pass. The
records are written by `dispatch claim` itself into `memory_forge`'s forge, so a hook
reads exactly what a claim leaves.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from memory_forge import MemoryForge

from kluster.scripts.dispatch import brief, claim, hooks, serialized
from kluster.scripts.dispatch.forge import ForgeError, Milestone

PARALLEL = 'Parallel — non-blocking'
LABELS = ('area/apps', 'kind/task')
#: The rfc-008 slice that shipped as Aetf/kluster#922 under the rfc's own issue.
SLICE = 922
RFC_ISSUE = 78
BRANCH = 'b-s12-blog'
WORKSPACE = 'b-s12'
HEAD = 'c0ffee' * 6 + 'c0ff'
MAIN_TIP = 'b2e39b774a3921ce12e5609d3df517912444db27'


@dataclass
class Repository:
    """A `jj` repository: each workspace and the parents of its working-copy commit."""

    parents_: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def workspaces(self) -> list[str]:
        return ['default', *self.parents_]

    def parents(self, workspace: str) -> list[str]:
        return self.parents_[workspace]


@pytest.fixture
def forge() -> MemoryForge:
    forge = MemoryForge()
    forge.milestones_[PARALLEL] = Milestone(PARALLEL, open=True, description='Beside the mainline.')
    forge.add(SLICE, PARALLEL, *LABELS, title='rfc-008 slice 12: the blog')
    forge.add(RFC_ISSUE, PARALLEL, *LABELS, title='rfc-008: workload migration')
    return forge


@pytest.fixture
def repository() -> Repository:
    return Repository({WORKSPACE: [HEAD]})


def _brief(tmp_path: Path, issue: int) -> Path:
    path = tmp_path / 'briefs' / f'{WORKSPACE}.md'
    path.parent.mkdir(exist_ok=True)
    _ = path.write_text(
        f'---\nissue: {issue}\nrole: builder\ndone: The blog merges.\n'
        f'owned: [src/kluster/components/apps/blog/]\ncloses: true\n'
        f'workspace: {WORKSPACE}\nbranch: {BRANCH}\n---\n\nBuild the blog.\n'
    )
    return path


def _claimed(forge: MemoryForge, path: Path) -> None:
    """`dispatch claim` of the brief at `path`, as the dispatcher runs it."""
    read = brief.read(path.read_bytes())
    _ = claim.claim(forge, claim.Dispatch(brief=read, session='session-a'), read.front.issue, serialized.read())


def _event(tool: str, tool_input: Mapping[str, object], *, agent_type: str | None = None, cwd: Path) -> str:
    event: dict[str, object] = {
        'session_id': 'abc123',
        'prompt_id': '550e8400-e29b-41d4-a716-446655440000',
        'transcript_path': '/home/user/.claude/projects/my-project/transcript.jsonl',
        'cwd': str(cwd),
        'permission_mode': 'auto',
        'hook_event_name': 'PreToolUse',
        'tool_name': tool,
        'tool_input': dict(tool_input),
        'tool_use_id': 'toolu_01ABC123',
    }
    if agent_type is not None:
        event |= {'agent_id': 'agent-def456', 'agent_type': agent_type}
    return json.dumps(event)


def _bash(command: str, *, agent_type: str | None = None, cwd: Path) -> str:
    return _event(
        'Bash',
        {'command': command, 'description': 'a command', 'timeout': 120000, 'run_in_background': False},
        agent_type=agent_type,
        cwd=cwd,
    )


@dataclass(frozen=True)
class Ran:
    status: int
    #: The reason of a `deny` decision; None where the hook printed none.
    denied: str | None
    stderr: str


def _stand_in(monkeypatch: pytest.MonkeyPatch, forge: MemoryForge, repository: Repository) -> None:
    """Make the cases' forge and repository the ones `main` reaches."""

    def jj(_root: Path) -> Repository:
        return repository

    monkeypatch.setattr(hooks, 'HookGh', lambda: forge)
    monkeypatch.setattr(hooks, 'Jj', jj)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook: str,
    event: str,
    forge: MemoryForge,
    repository: Repository,
) -> Ran:
    """Run `dispatch-hook <hook>` on `event`, the way Claude Code runs a hook, against the cases' forge."""
    _stand_in(monkeypatch, forge, repository)
    monkeypatch.setattr('sys.stdin', io.StringIO(event))
    status = hooks.main([hook])
    out, err = capsys.readouterr()
    if not out.strip():
        return Ran(status, None, err)
    decision = json.loads(out)['hookSpecificOutput']
    assert (decision['hookEventName'], decision['permissionDecision']) == ('PreToolUse', 'deny')
    return Ran(status, str(decision['permissionDecisionReason']), err)


# -- hook agent ----------------------------------------------------------------


def _spawn(prompt: str, kind: str = hooks.BUILDER, *, cwd: Path) -> str:
    return _event(
        'Agent',
        {'prompt': prompt, 'description': 'Build the blog', 'subagent_type': kind, 'model': 'opus'},
        cwd=cwd,
    )


def test_a_builder_spawned_for_a_slice_brief_with_no_issue_of_its_own_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    """rfc-008 slice 12, which shipped as Aetf/kluster#922 under the RFC's issue (D§5)."""
    path = _brief(tmp_path, RFC_ISSUE)
    ran = _run(monkeypatch, capsys, 'agent', _spawn(f'Your brief is {path}.', cwd=tmp_path), forge, repository)
    assert ran.status == hooks.RAN
    assert ran.denied is not None
    assert f'#{RFC_ISSUE}' in ran.denied


def test_a_builder_spawned_under_its_briefs_record_goes_ahead(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    path = _brief(tmp_path, SLICE)
    _claimed(forge, path)
    ran = _run(monkeypatch, capsys, 'agent', _spawn(f'Your brief is {path}.', cwd=tmp_path), forge, repository)
    assert ran == Ran(hooks.RAN, None, '')


def test_a_brief_edited_after_its_claim_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    path = _brief(tmp_path, SLICE)
    _claimed(forge, path)
    _ = path.write_text(path.read_text() + 'And the archive.\n')
    ran = _run(monkeypatch, capsys, 'agent', _spawn(f'Your brief is {path}.', cwd=tmp_path), forge, repository)
    assert ran.denied is not None


def test_an_agent_this_repository_does_not_define_is_not_held(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    event = _spawn('Find every caller of `resolve`.', 'Explore', cwd=tmp_path)
    assert _run(monkeypatch, capsys, 'agent', event, forge, repository) == Ran(hooks.RAN, None, '')


def test_a_hook_that_cannot_read_the_forge_refuses_the_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    def unreachable(number: int) -> object:
        raise ForgeError(f'`gh issue view {number}` failed: connection refused')

    monkeypatch.setattr(forge, 'issue', unreachable)
    path = _brief(tmp_path, SLICE)
    ran = _run(monkeypatch, capsys, 'agent', _spawn(f'Your brief is {path}.', cwd=tmp_path), forge, repository)
    assert (ran.status, ran.denied) == (hooks.BLOCK, None)
    assert 'connection refused' in ran.stderr


# -- hook pr-create ------------------------------------------------------------

_SECTIONS = """
## Live

Unproven live: the first `pulumi up` of `apps` confirms the blog answers.

## Fails without

Dropping the route reddens `test_the_blog_serves_its_site`.

## Sweep

612 tracked files, `blog.{0,120}route`, positive control "the blog's route".

## Gate

Clean.
"""


def _opening(body: str, tmp_path: Path) -> str:
    path = tmp_path / 'body.md'
    _ = path.write_text(body)
    return f'gh pr create --repo Aetf/kluster --head {BRANCH} --title "rfc-008 slice 12: the blog" --body-file {path}'


def test_a_pull_request_referring_to_the_rfc_under_its_own_issues_record_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    """Aetf/kluster#922's description, under a record for its own issue and `closes: true` (D§5)."""
    _claimed(forge, _brief(tmp_path, SLICE))
    command = _opening(f'Refs Aetf/kluster-ops#{RFC_ISSUE}\n{_SECTIONS}', tmp_path)
    ran = _run(
        monkeypatch, capsys, 'pr-create', _bash(command, agent_type=hooks.BUILDER, cwd=tmp_path), forge, repository
    )
    assert ran.denied is not None
    assert f'Closes Aetf/kluster-ops#{SLICE}' in ran.denied


def test_a_pull_request_closing_its_issue_in_the_form_opens(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    _claimed(forge, _brief(tmp_path, SLICE))
    command = _opening(f'Closes Aetf/kluster-ops#{SLICE}\n{_SECTIONS}', tmp_path)
    ran = _run(
        monkeypatch, capsys, 'pr-create', _bash(command, agent_type=hooks.BUILDER, cwd=tmp_path), forge, repository
    )
    assert ran == Ran(hooks.RAN, None, '')


def test_a_relative_description_file_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    """The command may `cd` first, so the event's `cwd` does not say which file a relative path names."""
    _claimed(forge, _brief(tmp_path, SLICE))
    _ = (tmp_path / 'body.md').write_text(f'Closes Aetf/kluster-ops#{SLICE}\n{_SECTIONS}')
    command = (
        f'cd {tmp_path} && gh pr create --repo Aetf/kluster --head {BRANCH} --title "the blog" --body-file body.md'
    )
    ran = _run(monkeypatch, capsys, 'pr-create', _bash(command, cwd=tmp_path), forge, repository)
    assert ran.denied is not None
    assert 'absolute' in ran.denied


def test_a_pull_request_on_a_branch_no_record_names_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    command = _opening(f'Closes Aetf/kluster-ops#{SLICE}\n{_SECTIONS}', tmp_path)
    ran = _run(monkeypatch, capsys, 'pr-create', _bash(command, cwd=tmp_path), forge, repository)
    assert ran.denied is not None
    assert BRANCH in ran.denied


@pytest.mark.parametrize(
    'command',
    [
        pytest.param('echo "$HOME"', id='a variable, which the hook filter lets through'),
        pytest.param('gh pr view 922 --repo Aetf/kluster', id='another gh pr command'),
        pytest.param("rg -n 'gh pr create' docs/", id='a search for the words'),
    ],
)
def test_a_command_that_opens_no_pull_request_is_not_held(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    command: str,
) -> None:
    assert _run(monkeypatch, capsys, 'pr-create', _bash(command, cwd=tmp_path), forge, repository) == Ran(
        hooks.RAN, None, ''
    )


# -- hook handback -------------------------------------------------------------


def _report(message: str, *, cwd: Path) -> str:
    return _event('SubagentHandback', {'message': message}, agent_type=hooks.BUILDER, cwd=cwd)


@pytest.fixture
def opened(tmp_path: Path, forge: MemoryForge) -> Path:
    """The slice claimed, and its pull request #940 open on the forge at `HEAD`; the brief's path."""
    path = _brief(tmp_path, SLICE)
    _claimed(forge, path)
    forge.heads[940] = hooks.PullHead(number=940, branch=BRANCH, head=HEAD, changed_files=4)
    return path


@pytest.mark.parametrize(
    ('head', 'pull', 'names'),
    [
        pytest.param(HEAD[:12], hooks.PullHead(940, BRANCH, HEAD, 4), 'in full', id='abbreviated'),
        pytest.param(MAIN_TIP, hooks.PullHead(940, BRANCH, HEAD, 4), MAIN_TIP, id='not the forge head'),
        pytest.param(MAIN_TIP, hooks.PullHead(940, BRANCH, MAIN_TIP, 0), 'changes no file', id='work left in @'),
    ],
)
def test_a_report_whose_head_is_not_the_work_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    opened: Path,
    head: str,
    pull: hooks.PullHead,
    names: str,
) -> None:
    """An abbreviated head, a head the forge does not hold, and a branch on `main`'s tip (D§5)."""
    forge.heads[940] = pull
    message = f'Brief: {opened}\nPR: https://github.com/Aetf/kluster/pull/940\nHead: {head}\n'
    ran = _run(monkeypatch, capsys, 'handback', _report(message, cwd=tmp_path), forge, repository)
    assert ran.denied is not None
    assert names in ran.denied


def test_a_report_naming_the_forge_head_which_is_the_workspaces_parent_goes_back(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    opened: Path,
) -> None:
    message = f'Brief: {opened}\nPR: https://github.com/Aetf/kluster/pull/940\nHead: {HEAD}\n'
    ran = _run(monkeypatch, capsys, 'handback', _report(message, cwd=tmp_path), forge, repository)
    assert ran == Ran(hooks.RAN, None, '')


def test_a_report_whose_bookmark_was_left_on_an_earlier_round_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    opened: Path,
) -> None:
    """The forge holds the earlier round's head, which the report names, and the work sits at `@-`."""
    message = f'Brief: {opened}\nPR: https://github.com/Aetf/kluster/pull/940\nHead: {HEAD}\n'
    moved_on = Repository({WORKSPACE: ['d1ce' * 10]})
    ran = _run(monkeypatch, capsys, 'handback', _report(message, cwd=tmp_path), forge, moved_on)
    assert ran.denied is not None
    assert '`@-`' in ran.denied


@pytest.mark.parametrize(('standing', 'refused'), [(True, True), (False, False)])
def test_a_finished_report_with_no_pull_request_goes_back_once_its_workspace_is_gone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    opened: Path,
    standing: bool,
    refused: bool,
) -> None:
    repository = Repository({WORKSPACE: [MAIN_TIP]} if standing else {})
    message = f'Brief: {opened}\nDone: the blog was already served as the brief asks, so there is nothing to merge.\n'
    ran = _run(monkeypatch, capsys, 'handback', _report(message, cwd=tmp_path), forge, repository)
    assert (ran.denied is not None) is refused


def test_a_report_that_stops_to_ask_goes_back_with_its_workspace_standing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    opened: Path,
) -> None:
    """A stop and report keeps its workspace, and may hold work that is not pushed yet."""
    message = f'Stopped: the work needs docs/operations.md, outside the paths of {opened}.\n'
    ran = _run(monkeypatch, capsys, 'handback', _report(message, cwd=tmp_path), forge, repository)
    assert ran == Ran(hooks.RAN, None, '')


def test_a_report_of_an_agent_other_than_a_builder_is_not_held(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
) -> None:
    event = _event('SubagentHandback', {'message': 'Review posted.'}, agent_type='kluster-tl', cwd=tmp_path)
    assert _run(monkeypatch, capsys, 'handback', event, forge, repository) == Ran(hooks.RAN, None, '')


def test_a_report_naming_no_brief_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    opened: Path,
) -> None:
    message = f'PR: https://github.com/Aetf/kluster/pull/940\nHead: {HEAD}\n'
    ran = _run(monkeypatch, capsys, 'handback', _report(message, cwd=tmp_path), forge, repository)
    assert ran.denied is not None
    assert 'brief' in ran.denied


# -- hook builder-bash ---------------------------------------------------------


@pytest.mark.parametrize(
    'command',
    [
        'jj git fetch',
        'cd /home/user/kluster/.claude/workspaces/b-s12 && mise x -- jj --no-pager git fetch --remote origin',
        'timeout 60 git -C /home/user/kluster fetch origin',
        'echo "$(jj git fetch)"',
        "bash -c 'cd /tmp && git fetch'",
        'mise x uv -- uv run dispatch claim 922 /var/lib/build/kluster/briefs/b-s12.md',
        'dispatch abandon 922',
    ],
)
def test_a_builder_neither_fetches_nor_takes_a_dispatchers_step(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    command: str,
) -> None:
    event = _bash(command, agent_type=hooks.BUILDER, cwd=tmp_path)
    assert _run(monkeypatch, capsys, 'builder-bash', event, forge, repository).denied is not None


@pytest.mark.parametrize(
    'command',
    [
        'jj git push --bookmark b-s12-blog',
        "rg -n 'jj git fetch' docs/",
        "cat > .claude/body.md <<'EOF'\nThe builder's hook refuses jj git fetch.\nEOF\njj st",
        'mise x uv -- uv run dispatch audit',
    ],
)
def test_a_builders_other_commands_go_ahead(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    forge: MemoryForge,
    repository: Repository,
    command: str,
) -> None:
    event = _bash(command, agent_type=hooks.BUILDER, cwd=tmp_path)
    assert _run(monkeypatch, capsys, 'builder-bash', event, forge, repository) == Ran(hooks.RAN, None, '')


# -- hook session-start --------------------------------------------------------


def _session_start(monkeypatch: pytest.MonkeyPatch, forge: MemoryForge, repository: Repository) -> int:
    _stand_in(monkeypatch, forge, repository)
    return hooks.main([hooks.SESSION_START])


def test_a_session_starts_with_the_audits_findings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forge: MemoryForge,
    repository: Repository,
) -> None:
    forge.add(1, PARALLEL, 'area/apps')
    assert _session_start(monkeypatch, forge, repository) == hooks.RAN
    assert '- unlabeled: #1 lacks a label from `kind/*`' in capsys.readouterr().out.splitlines()


def test_a_session_starts_when_the_audit_cannot_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    forge: MemoryForge,
    repository: Repository,
) -> None:
    def unreachable(**_: object) -> object:
        raise ForgeError('`gh issue list` failed: connection refused')

    monkeypatch.setattr(forge, 'issues', unreachable)
    assert _session_start(monkeypatch, forge, repository) == hooks.RAN
    assert 'could not run' in capsys.readouterr().out
