"""Which pull requests `noop-automerge.yml` lets merge with nobody reading them.

`classify` admits a pull request only when every path it changes is on the
workflow's allow-list, and every other pull request waits for a human
(framework/ci.md §3). The cases below run the step as it is written -- bash in
YAML, the runner's own invocation -- with the one command that reaches the
network, `gh`, replaced by a fake that answers the way the API does. The list
itself is read out of the workflow and never restated here, so a case holds the
verdict the runner would reach and not a copy of how it reaches it.

A class the list admits has one case, and so does every class it refuses: the
trust anchors a stack program never reads, which preview empty however they
change and which a deny-list admitted until somebody thought to name them; a
rename away from one of those into the list, which deletes it; a documentation
change, which no preview measures; and the paths that begin or end like an
admitted one without being one.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from workflow_files import GITHUB, mapping, read_workflow, workflow_jobs

WORKFLOW = GITHUB / 'workflows' / 'noop-automerge.yml'

#: What the fake answers for, spelled as the step asks: the paged read of the
#: changed files and the pull request's own report of them. It serves what the
#: API serves -- JSON, a page per hundred entries, and for `pr view` the fields
#: asked for and no others -- and applies the step's own `--jq` filter to it
#: with `jq`, as `gh` does, page by page. So the filter the step writes is part
#: of what a case runs, and a case can say anything the API can, a rename
#: included. Anything else is an invocation the step is not meant to make, and
#: exits in a way no case expects.
FAKE_GH = r"""#!/bin/bash
set -eo pipefail
[ -n "$FAKE_GH_FAILS" ] && exit 1
filter=
for ((i = 1; i < $#; i++)); do
  if [ "${!i}" = --jq ]; then
    next=$((i + 1))
    filter=${!next}
  fi
done
if [ -z "$filter" ]; then
  echo "gh called without --jq: $*" >&2
  exit 99
fi
if [ $# -eq 5 ] && [ "$1 $2 $3" = "api --paginate repos/$GH_REPO/pulls/$PR/files?per_page=100" ]; then
  jq -c '.[]' <<<"$FAKE_PAGES" | while IFS= read -r page; do
    jq -r "$filter" <<<"$page" || exit 98
  done
elif [ $# -eq 7 ] && [ "$1 $2 $3 $4" = "pr view $PR --json" ]; then
  jq --arg fields "$5" 'with_entries(select(.key as $k | $fields | split(",") | index($k)))' <<<"$FAKE_PR" \
    | jq -r "$filter"
else
  echo "unexpected gh invocation: $*" >&2
  exit 99
fi
"""

#: The page size the step asks for, which is what the fake splits the list by.
PER_PAGE = 100

#: What the event supplies to the workflow's `${{ }}` expressions, and so what a
#: case supplies in their place. A step or job variable holding an expression
#: not named here is one the cases do not know how to fill, and the case after
#: this fails on it rather than leaving the variable unset.
EVENT = {'GH_TOKEN', 'GH_REPO', 'PR', 'AUTHOR', 'HEAD_SHA', 'OVERRIDE', 'FORK', 'ADMIT'}

#: The head the event names, which the pull request reports unless a case says
#: the branch has moved since.
EVENT_HEAD = 'a' * 40


def _classify() -> tuple[dict[str, str], dict[str, str], str]:
    """The workflow's env, the deciding step's env, and the step's script."""
    workflow = read_workflow(WORKFLOW)
    job = workflow_jobs(workflow, 'noop-automerge.yml')['classify']
    listed = job['steps']
    assert isinstance(listed, list), 'classify has no list of steps'
    steps = [mapping(step, 'a step of classify') for step in cast('list[object]', listed)]
    (step,) = [step for step in steps if step.get('id') == 'classify']
    workflow_env = {name: str(value) for name, value in mapping(workflow['env'], 'env').items()}
    step_env = {name: str(value) for name, value in mapping(step['env'], 'classify env').items()}
    return workflow_env, step_env, str(step['run'])


@dataclass(frozen=True)
class Verdict:
    returncode: int
    outputs: dict[str, str]
    stdout: str
    stderr: str

    @property
    def noop(self) -> bool:
        """Whether the merge job's `if` would read the pull request as a candidate."""
        return self.outputs.get('noop') == 'true'


@dataclass(frozen=True)
class Renamed:
    """A rename, as the API lists it: one entry, the new path and the old."""

    previous: str
    filename: str


def _entry(change: str | Renamed) -> dict[str, str]:
    """A changed file as the list-files endpoint serves it."""
    if isinstance(change, Renamed):
        return {'filename': change.filename, 'status': 'renamed', 'previous_filename': change.previous}
    return {'filename': change, 'status': 'modified'}


#: `changed=COUNTED` reports as many changed files as the case lists.
COUNTED = object()


def _run(
    tmp_path: Path,
    files: list[str | Renamed],
    *,
    changed: object = COUNTED,
    head: str = EVENT_HEAD,
    admit: bool = False,
    fork: bool = False,
    override: bool = False,
    gh_fails: bool = False,
) -> Verdict:
    """Run `classify`'s deciding step the way the runner does, for a pull request changing `files`."""
    fake = tmp_path / 'bin' / 'gh'
    fake.parent.mkdir(parents=True)
    fake.write_text(FAKE_GH)
    fake.chmod(0o755)
    output = tmp_path / 'output'
    output.write_text('')

    workflow_env, step_env, script = _classify()
    event = {
        'GH_TOKEN': 'token',
        'GH_REPO': 'Aetf/kluster',
        'PR': '7',
        'AUTHOR': 'a-contributor',
        'HEAD_SHA': EVENT_HEAD,
        'OVERRIDE': str(override).lower(),
        'FORK': str(fork).lower(),
        'ADMIT': str(admit).lower(),
    }
    env = {
        **{name: value for name, value in {**workflow_env, **step_env}.items() if '${{' not in value},
        **event,
        'PATH': f'{fake.parent}{os.pathsep}{os.environ["PATH"]}',
        'GITHUB_OUTPUT': str(output),
        'FAKE_PAGES': json.dumps(
            [[_entry(change) for change in files[start : start + PER_PAGE]] for start in range(0, len(files), PER_PAGE)]
            or [[]]
        ),
        'FAKE_PR': json.dumps({'changedFiles': len(files) if changed is COUNTED else changed, 'headRefOid': head}),
        'FAKE_GH_FAILS': '1' if gh_fails else '',
    }
    done = subprocess.run(
        ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    outputs = dict(line.split('=', 1) for line in output.read_text().splitlines())
    return Verdict(done.returncode, outputs, done.stdout, done.stderr)


def test_the_step_the_cases_run_is_filled_the_way_the_runner_fills_it() -> None:
    """Every expression the step reads is one a case supplies, and the allow-list is a literal.

    A variable the cases left unset would read as empty to the script, which is
    a verdict about a pull request no event describes; and an allow-list
    computed from the event could not be read out of the file at all.
    """
    workflow_env, step_env, script = _classify()
    expressions = {name for name, value in {**workflow_env, **step_env}.items() if '${{' in value}

    assert expressions <= EVENT, f'filled from the event and by no case: {sorted(expressions - EVENT)}'
    assert '${{' not in step_env['ALLOWED']
    assert '${{' not in step_env['WITH_THE_BUMP']
    assert '${{' not in script


# --------------------------------------------------------------------------
# What the list admits.


def test_a_lockfile_refresh_is_a_candidate(tmp_path: Path) -> None:
    verdict = _run(tmp_path, ['uv.lock'])
    assert verdict.returncode == 0, verdict.stderr
    assert verdict.noop


def test_a_regeneration_of_the_bridged_sdks_in_renovates_bump_is_a_candidate(tmp_path: Path) -> None:
    """`sdks/` joins the list beside renovate's packages-only bump of `Pulumi.yaml`, which it regenerates."""
    verdict = _run(
        tmp_path, ['Pulumi.yaml', 'sdks/b2/pulumi_b2/provider.py', 'sdks/b2/pyproject.toml', 'uv.lock'], admit=True
    )
    assert verdict.returncode == 0, verdict.stderr
    assert verdict.noop


def test_a_rename_inside_the_list_is_a_candidate(tmp_path: Path) -> None:
    verdict = _run(
        tmp_path, ['Pulumi.yaml', Renamed('sdks/a/provider.py', 'sdks/b/provider.py'), 'uv.lock'], admit=True
    )
    assert verdict.returncode == 0, verdict.stderr
    assert verdict.noop


def test_a_list_longer_than_a_page_is_read_whole(tmp_path: Path) -> None:
    """Every page is read and counted, so a long list of admitted paths is still a candidate.

    `Pulumi.yaml` sorts last, on the second page, so the bump is found only
    when that page is read.
    """
    regenerated: list[str | Renamed] = [f'sdks/b2/pulumi_b2/module_{index}.py' for index in range(PER_PAGE + 50)]
    verdict = _run(tmp_path, [*regenerated, 'Pulumi.yaml'], admit=True)
    assert verdict.returncode == 0, verdict.stderr
    assert verdict.noop


def test_renovates_bump_of_the_packages_block_is_a_candidate(tmp_path: Path) -> None:
    """`Pulumi.yaml` is on the list when the admission step admitted it, beside what the bump regenerated.

    The admission is decided in the step before this one, from the author and
    the two revisions; this step reads its verdict alone.
    """
    verdict = _run(tmp_path, ['Pulumi.yaml', 'sdks/b2/pulumi_b2/provider.py', 'uv.lock'], admit=True)
    assert verdict.returncode == 0, verdict.stderr
    assert verdict.noop


# --------------------------------------------------------------------------
# What the list refuses.


def test_pulumi_yaml_the_admission_did_not_admit_is_the_human_route(tmp_path: Path) -> None:
    """A change beyond `packages:`, or anybody's but renovate's, leaves `admit` false."""
    verdict = _run(tmp_path, ['Pulumi.yaml', 'uv.lock'], admit=False)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert "Pulumi.yaml is on the allow-list only in renovate's packages-only bump" in verdict.stdout


def test_the_bridged_sdks_without_renovates_bump_are_the_human_route(tmp_path: Path) -> None:
    """A change under `sdks/` on a pull request that does not bump the block is no regeneration, and waits.

    Nothing measures `sdks/`: the preview never renders it and `checks` reads
    only each SDK's `pulumi-plugin.json`. So it is admitted only as what
    sdk-regenerate.yml writes there, and anybody's other change to it --
    renovate's included -- is the human route, and the log says why.
    """
    verdict = _run(tmp_path, ['sdks/b2/pulumi_b2/provider.py', 'uv.lock'])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert "sdks/b2/pulumi_b2/provider.py is on the allow-list only in renovate's packages-only bump" in verdict.stdout


def test_the_bridged_sdks_in_renovates_pull_request_that_leaves_the_block_are_the_human_route(tmp_path: Path) -> None:
    """The admission compares documents, so it holds for a renovate pull request that never touched `Pulumi.yaml`.

    Lockfile maintenance is one: the block is equal at base and head because
    the file did not change, and `sdks/` beside it regenerated nothing.
    """
    verdict = _run(tmp_path, ['sdks/b2/pulumi_b2/provider.py', 'uv.lock'], admit=True)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert "sdks/b2/pulumi_b2/provider.py is on the allow-list only in renovate's packages-only bump" in verdict.stdout


def test_the_bridged_sdks_beside_a_change_to_pulumi_yaml_that_is_not_packages_only_are_the_human_route(
    tmp_path: Path,
) -> None:
    """A `Pulumi.yaml` change the admission refused carries no `sdks/` with it."""
    verdict = _run(tmp_path, ['sdks/b2/pulumi_b2/provider.py', 'Pulumi.yaml', 'uv.lock'], admit=False)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert "sdks/b2/pulumi_b2/provider.py is on the allow-list only in renovate's packages-only bump" in verdict.stdout


@pytest.mark.parametrize(
    'path',
    [
        'src/kluster/main.py',
        'Pulumi.physical.yaml',
        'pyproject.toml',
        '.github/workflows/deploy.yml',
        'docker/emailproxy.conf',
    ],
)
def test_a_path_a_stack_or_a_workflow_reads_is_the_human_route(path: str, tmp_path: Path) -> None:
    verdict = _run(tmp_path, ['uv.lock', path])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert f'{path} is not on the allow-list' in verdict.stdout


@pytest.mark.parametrize(
    'path',
    [
        'escrow/RECIPIENTS',
        'src/kluster/lib/state_backend/machine/operator-keys.txt',
        'src/kluster/lib/state_backend/machine/drill-recipient.txt',
        '.github/actions/zerotier/action.yml',
        '.github/actions/state-backend/action.yml',
        'mise.toml',
    ],
)
def test_a_trust_anchor_no_stack_reads_is_the_human_route(path: str, tmp_path: Path) -> None:
    """A path no stack program reads previews empty however it changed, so only a reader can hold it."""
    verdict = _run(tmp_path, ['uv.lock', path])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert f'{path} is not on the allow-list' in verdict.stdout


@pytest.mark.parametrize(
    'rename',
    [
        Renamed('.gitignore', 'sdks/.gitignore'),
        Renamed('escrow/RECIPIENTS', 'sdks/y'),
        Renamed('src/kluster/lib/state_backend/machine/drill-recipient.txt', 'sdks/z'),
    ],
    ids=lambda rename: rename.previous,
)
def test_a_rename_from_off_the_list_is_the_human_route(rename: Renamed, tmp_path: Path) -> None:
    """A rename deletes its old path, which no preview measures when no stack reads it.

    The API lists a rename once, under its new path, and counts it once, so
    the old path is held to the list only if the step reads it at all. The
    rename lands in `sdks/` beside renovate's bump, so its new path is on the
    list and the old one is what the step refuses.
    """
    verdict = _run(tmp_path, ['Pulumi.yaml', 'uv.lock', rename], admit=True)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert f'{rename.previous} is not on the allow-list' in verdict.stdout


def test_a_rename_out_of_the_list_is_the_human_route(tmp_path: Path) -> None:
    verdict = _run(tmp_path, [Renamed('sdks/b2/README.md', 'docs/b2.md')])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert 'docs/b2.md is not on the allow-list' in verdict.stdout


@pytest.mark.parametrize('path', ['docs/framework/ci.md', 'AGENTS.md', '.vscode/ltex.dictionary.en-US.txt'])
def test_documentation_is_the_human_route(path: str, tmp_path: Path) -> None:
    """No preview measures prose, so a reader is the only thing that can hold it."""
    verdict = _run(tmp_path, [path])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert f'{path} is not on the allow-list' in verdict.stdout


@pytest.mark.parametrize('path', ['uv.lock.orig', 'packages/uv.lock', 'sdks', 'src/sdks/b2/provider.py'])
def test_a_path_that_only_resembles_an_admitted_one_is_the_human_route(path: str, tmp_path: Path) -> None:
    """Each entry is matched against the whole path, not found inside it."""
    verdict = _run(tmp_path, [path])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert f'{path} is not on the allow-list' in verdict.stdout


# --------------------------------------------------------------------------
# What stands the route down before the list is consulted.


def test_a_fork_is_the_human_route(tmp_path: Path) -> None:
    verdict = _run(tmp_path, ['uv.lock'], fork=True)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop


def test_an_expect_changes_label_is_the_human_route(tmp_path: Path) -> None:
    verdict = _run(tmp_path, ['uv.lock'], override=True)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop


def test_a_list_shorter_than_the_pull_request_is_the_human_route(tmp_path: Path) -> None:
    """A path the read never returned is one the list never refused, so a short read refuses by itself."""
    verdict = _run(tmp_path, ['uv.lock'], changed=150)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert 'Read 1 of the 150 changed paths' in verdict.stdout


def test_a_branch_that_moved_since_the_event_is_the_human_route(tmp_path: Path) -> None:
    """The verdict is for the head the run was started for, and the list read may be a newer one's."""
    verdict = _run(tmp_path, ['uv.lock'], head='b' * 40)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert f"head is '{'b' * 40}', not {EVENT_HEAD}" in verdict.stdout


def test_the_merge_lands_the_head_the_run_classified_and_no_other() -> None:
    """A re-run reuses its finished jobs' verdicts, so the merge names the head they were about.

    `--match-head-commit` makes GitHub refuse the merge once the branch has
    moved; without it, a re-run of an old head's run merges whatever the branch
    carries by then.
    """
    workflow = read_workflow(WORKFLOW)
    job = workflow_jobs(workflow, 'noop-automerge.yml')['merge']
    listed = job['steps']
    assert isinstance(listed, list), 'merge has no list of steps'
    runs = [str(mapping(step, 'a step of merge').get('run', '')) for step in cast('list[object]', listed)]
    (merging,) = [run for run in runs if 'gh pr merge' in run]

    assert '--match-head-commit "$HEAD_SHA"' in merging
    assert mapping(workflow['env'], 'env')['HEAD_SHA'] == '${{ github.event.pull_request.head.sha }}'


def test_a_count_that_is_not_a_number_is_the_human_route(tmp_path: Path) -> None:
    """A count the list cannot be held to refuses rather than being walked past."""
    verdict = _run(tmp_path, ['uv.lock'], changed=None)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert 'Read 1 of the null changed paths' in verdict.stdout


def test_an_empty_list_is_the_human_route(tmp_path: Path) -> None:
    verdict = _run(tmp_path, [])
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop


def test_a_read_that_failed_is_the_human_route(tmp_path: Path) -> None:
    verdict = _run(tmp_path, ['uv.lock'], gh_fails=True)
    assert verdict.returncode == 0, verdict.stderr
    assert not verdict.noop
    assert 'could not be read' in verdict.stdout
