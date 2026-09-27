"""What a session takes from the repository, held where its failure would be silent.

A Claude Code cloud session installs its tools through
`deploy/cloud-session/toolchain.sh` (framework/dispatch.md §1.4). Its mise is
CI's: a workflow step and the script disagreeing on it is a cloud gate that
runs a mise CI never ran, and nothing on a pull request would show it. The
`SessionStart` hook that runs the script must run it in a cloud session and
in no other, and `.gitignore` must let the tracked `.claude/` paths through
while keeping every other one out; each failure would pass unseen.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
from workflow_files import github_name, mapping, read_workflow, workflows_and_actions

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / 'deploy' / 'cloud-session' / 'toolchain.sh'

#: The custom manager's pattern for the script's mise pin, as renovate.json5
#: holds it: renovate's regex engine spells a named group `(?<...>`.
MISE_MATCH_STRING = r'mise_version=(?<currentValue>\d[\d.]*)'


def _script_mise_version() -> str:
    """The mise version the script installs, found the way renovate finds it."""
    found = re.search(MISE_MATCH_STRING.replace('(?<', '(?P<'), SCRIPT.read_text())
    assert found is not None, f'{SCRIPT.relative_to(ROOT)} carries no mise pin renovate can read'
    return found.group('currentValue')


def _mise_action_versions() -> dict[str, object]:
    """The `version` input of every `jdx/mise-action` step, by file, job and step position."""
    versions: dict[str, object] = {}
    for path in workflows_and_actions():
        document = read_workflow(path)
        if path.parent.name == 'workflows':
            jobs = mapping(document.get('jobs'), f'{github_name(path)} jobs:')
            groups = {name: mapping(job, f'{github_name(path)} job {name}').get('steps') for name, job in jobs.items()}
        else:
            groups = {'runs': mapping(document.get('runs'), f'{github_name(path)} runs:').get('steps')}
        for name, steps in groups.items():
            for index, step in enumerate(cast('list[object]', steps or [])):
                step_map = mapping(step, f'{github_name(path)} {name} step {index}')
                uses = step_map.get('uses')
                if isinstance(uses, str) and uses.startswith('jdx/mise-action@'):
                    inputs = mapping(step_map.get('with', {}), f'{github_name(path)} {name} step {index} with:')
                    versions[f'{github_name(path)} {name} step {index}'] = inputs.get('version')
    return versions


def test_the_cloud_session_installs_the_mise_every_workflow_installs() -> None:
    """One mise for CI and the cloud: the ltex install stays off the GitHub API only from a floor release on."""
    versions = _mise_action_versions()

    # A census that found no step would hold nothing.
    assert versions
    assert versions == dict.fromkeys(versions, _script_mise_version())


def test_renovate_reads_the_script_pin_as_the_workflows_mise() -> None:
    """The manager reads the script's pin, under the workflows' dependency name, as a bare version.

    Held as text, because there is no JSON5 parser here: the manager's entry
    is the braces around its pattern, which is written in `json.dumps`'s
    escaping. Its file pattern has to reach the script, its name has to be the
    one the toolchain group matches on, and its version extraction has to turn
    the release tag into the bare number the script writes. Any of the three
    wrong leaves the pin behind while the workflows move.
    """
    config = (ROOT / 'renovate.json5').read_text()
    pattern = json.dumps(MISE_MATCH_STRING)

    assert config.count(pattern) == 1
    at = config.index(pattern)
    entry = config[config.rindex('{', 0, at) : config.index('}', at)]

    assert "depNameTemplate: 'jdx/mise'" in entry
    assert "datasourceTemplate: 'github-releases'" in entry

    # JSON5 single quotes around renovate's `/regex/` form, backslashes doubled.
    files = re.findall(r"managerFilePatterns: \[\s*'/((?:[^'\\]|\\.)*)/',\s*\]", entry)
    assert len(files) == 1
    assert re.fullmatch(files[0].replace('\\\\', '\\'), str(SCRIPT.relative_to(ROOT)))

    extracts = re.findall(r"extractVersionTemplate: '((?:[^'\\]|\\.)*)'", entry)
    assert len(extracts) == 1
    tag = re.fullmatch(extracts[0].replace('(?<', '(?P<'), f'v{_script_mise_version()}')
    assert tag is not None
    assert tag.group('version') == _script_mise_version()


#: The values a local session can carry for `CLAUDE_CODE_REMOTE`: absent, and
#: every spelling that is not the cloud's exact `true`.
LOCAL_REMOTE_VALUES = (None, '', 'false', '1', 'TRUE')


def _session_start_commands() -> list[str]:
    """Every `SessionStart` command hook in the project settings."""
    settings = mapping(json.loads((ROOT / '.claude' / 'settings.json').read_text()), 'settings.json')
    hooks = mapping(settings.get('hooks'), 'settings.json hooks')
    commands: list[str] = []
    for group in cast('list[object]', hooks.get('SessionStart') or []):
        for hook in cast('list[object]', mapping(group, 'SessionStart group').get('hooks') or []):
            entry = mapping(hook, 'SessionStart hook')
            if entry.get('type') == 'command':
                commands.append(cast('str', entry['command']))
    return commands


def _runs_the_script(command: str, tmp_path: Path, remote: str | None) -> bool:
    """Whether `command`, run as Claude Code runs a hook, reaches the toolchain script.

    The project directory is a stand-in whose script only leaves a marker, so a
    guard that fails open installs nothing here: it shows as the marker.
    """
    project = tmp_path / f'project-{remote}'
    script = project / 'deploy' / 'cloud-session' / 'toolchain.sh'
    script.parent.mkdir(parents=True)
    marker = project / 'ran'
    script.write_text(f'touch {marker}\n')
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'CLAUDE_PROJECT_DIR': str(project)}
    if remote is not None:
        env['CLAUDE_CODE_REMOTE'] = remote
    # Hooks run in the session's working directory, so a command written
    # relative to it must meet the stand-in, not the checkout under test.
    completed = subprocess.run(
        ['sh', '-c', command], cwd=project, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    return marker.exists()


@pytest.mark.parametrize('remote', LOCAL_REMOTE_VALUES)
def test_a_local_session_start_runs_no_installer(tmp_path: Path, remote: str | None) -> None:
    """A dropped or inverted guard would install into every local session, and rewrite the operator's mise."""
    commands = _session_start_commands()

    assert commands
    assert not any(_runs_the_script(command, tmp_path / str(index), remote) for index, command in enumerate(commands))


def test_a_cloud_session_start_runs_the_installer(tmp_path: Path) -> None:
    """The positive control: the same stand-in, reached when the cloud's `true` is set."""
    commands = _session_start_commands()

    assert commands
    assert all(_runs_the_script(command, tmp_path / str(index), 'true') for index, command in enumerate(commands))


#: Scratch an agent or tool writes under `.claude/`, which `.gitignore` must keep out.
SCRATCH = (
    '.claude/workspaces/x/.claude/agents/a.md',
    '.claude/workspaces/x/CLAUDE.md',
    '.claude/worktrees/w/f',
    '.claude/ltex.json',
    '.claude/mutation/orig',
    '.claude/settings.local.json',
    '.claude/toolchain.log',
)
#: What the repository tracks under `.claude/`, which `.gitignore` must let through.
TRACKED = (
    '.claude/agents/kluster-builder.md',
    '.claude/settings.json',
)


@pytest.mark.parametrize('path', SCRATCH + TRACKED)
def test_gitignore_keeps_scratch_out_and_lets_the_tracked_paths_through(tmp_path: Path, path: str) -> None:
    """Evaluated in a repository of its own, so no enclosing checkout answers instead.

    Inside a `jj` workspace git walks up to the primary checkout and re-roots
    every path (framework/dispatch.md §1.2); a fresh repository holding only
    `.gitignore` has no such parent to answer for it. No global or system git
    configuration is read either, since a user's own excludes file would
    answer beside this one.
    """
    env = {
        'PATH': os.environ['PATH'],
        'HOME': str(tmp_path),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
    }
    subprocess.run(['git', 'init', '-q', str(tmp_path)], env=env, check=True, timeout=60)
    shutil.copy(ROOT / '.gitignore', tmp_path / '.gitignore')
    (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / path).touch()

    completed = subprocess.run(
        ['git', 'check-ignore', '--no-index', '-q', path], cwd=tmp_path, env=env, check=False, timeout=60
    )

    assert completed.returncode in (0, 1)
    assert (completed.returncode == 0) == (path in SCRATCH)
