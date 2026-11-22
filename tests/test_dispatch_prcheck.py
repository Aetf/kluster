"""`dispatch gates`, its deterministic half, over commits of a scratch repository and a forge of the cases' own.

The change is read by the `git` the gates run, from a repository whose base
and head the case commits, so a hunk is placed where `git diff` places it.
The dispatch it is held to is the record `dispatch claim` posts into
`memory_forge`'s forge. The owned-paths pair from the gates design
(Aetf/kluster-ops#917, D§5) is here, with the cases for the host names and
the sweep that make each of them fail without its check.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import process_sessions
import pytest
from memory_forge import MemoryForge

from kluster.scripts.dispatch import brief, claim, prcheck
from kluster.scripts.dispatch.forge import Milestone
from kluster.scripts.dispatch.prcheck import Check, Git, PullRequest

#: Each git call's bound: a stop-loss, which nothing asserts on.
GIT_TIMEOUT = 10

PARALLEL = 'Parallel — non-blocking'
ISSUE = 909
PULL = 4909
BRANCH = 'b-909-ops'
SESSION = 'session-of-the-dispatcher'

#: A runbook of three sections under one chapter, each four lines long.
OPERATIONS = '\n'.join(
    [
        '# Operations',
        '',
        '## 3. Running it',
        '',
        *(
            line
            for number in ('3.4', '3.5', '3.6')
            for line in (f'### {number} Step {number}', '', f'What step {number} does.', '')
        ),
    ]
)
BASE = {
    'docs/operations.md': OPERATIONS,
    'docs/physical/bring-up.md': '# Bring-up\n\nThe first boot.\n',
    'src/app.py': 'URL = "https://github.com/Aetf/kluster"\n',
}

#: A description that holds the form apart from what a case puts in it.
BODY = f"""Closes Aetf/kluster-ops#{ISSUE}

## Live

Not provider-facing.

## Fails without

No test: prose.

## Sweep

{{sweep}}

## Gate

Passed.

{{extra}}
"""
SWEEP = '- `step 3\\.5.{0,40}does`: control: "step 3.5 does". Over 3 files.'


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty scratch repository, with the caller's git kept out of it."""
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('HOME', str(tmp_path))
    for role in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv(f'GIT_{role}_NAME', 'gates probe')
        monkeypatch.setenv(f'GIT_{role}_EMAIL', 'probe@example.invalid')
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q')
    return repo


def _git(repo: Path, *arguments: str) -> str:
    return process_sessions.run(['git', *arguments], cwd=repo, text=True, check=True, timeout=GIT_TIMEOUT).stdout


def _commit(repo: Path, files: Mapping[str, str | None]) -> str:
    """Write `files` (a `None` deletes the path), commit everything, and return the commit."""
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text)
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', 'a commit')
    return _git(repo, 'rev-parse', 'HEAD').strip()


@dataclass
class PullForge(MemoryForge):
    """`memory_forge`'s forge, with each pull request read whole."""

    requests: dict[int, PullRequest] = field(default_factory=dict[int, PullRequest])

    def pull_request(self, number: int) -> PullRequest:
        return self.requests[number]


def _claimed(tmp_path: Path, owned: str) -> PullForge:
    """A forge whose issue carries the live record of a builder owning `owned` on `BRANCH`."""
    forge = PullForge()
    forge.milestones_[PARALLEL] = Milestone(PARALLEL, open=True, description='Beside the mainline.')
    forge.add(ISSUE, PARALLEL, 'area/docs', 'kind/task')
    path = tmp_path / 'brief.md'
    _ = path.write_text(
        f'---\nissue: {ISSUE}\nrole: builder\ndone: The runbook merges.\nowned: {owned}\ncloses: true\n'
        f'branch: {BRANCH}\n---\n\nWrite the runbook.\n'
    )
    dispatch = claim.Dispatch(brief=brief.read(path.read_bytes()), session=SESSION)
    _ = claim.claim(forge, dispatch, ISSUE, frozenset({'AGENTS.md'}))
    return forge


def _gates(
    tmp_path: Path,
    repository: Path,
    head: Mapping[str, str | None],
    *,
    owned: str = '[docs/operations.md#§3.5]',
    sweep: str = SWEEP,
    extra: str = '',
) -> prcheck.Report:
    """The gates' report on a pull request that changes `head` over `BASE`, under a record owning `owned`."""
    base = _commit(repository, BASE)
    tip = _commit(repository, head)
    forge = _claimed(tmp_path, owned)
    body = BODY.format(sweep=sweep, extra=extra)
    forge.requests[PULL] = PullRequest(PULL, 'docs: the runbook', body, 'OPEN', BRANCH, tip, base, ())
    return prcheck.gates(forge, Git(repository), PULL)


def _findings(report: prcheck.Report, check: Check) -> list[str]:
    return [finding for found, finding in report.findings if found is check]


def test_a_path_outside_the_brief_and_a_hunk_outside_its_section_are_found(tmp_path: Path, repository: Path) -> None:
    # Aetf/kluster#909: `docs/physical/bring-up.md` was outside the brief, and
    # the brief owned `docs/operations.md` in its §3.5 only.
    report = _gates(
        tmp_path,
        repository,
        {
            'docs/operations.md': OPERATIONS.replace('What step 3.6 does.', 'What step 3.6 does now.'),
            'docs/physical/bring-up.md': '# Bring-up\n\nThe first boot, and the second.\n',
        },
    )

    assert _findings(report, Check.OWNED) == [
        'docs/operations.md:15–15 lies outside §3.5, lines 9–12 at the base',
        'docs/physical/bring-up.md is outside the owned paths',
    ]


def test_a_new_file_the_description_names_and_edits_within_the_section_pass(tmp_path: Path, repository: Path) -> None:
    # §3.5 runs from its heading at line 9 to line 12, the blank line before
    # §3.6: an edit of its line, and lines inserted after its last, are its own.
    edited = OPERATIONS.replace('What step 3.5 does.', 'What step 3.5 does now.').replace(
        '### 3.6', 'Appended.\n\n### 3.6'
    )
    report = _gates(
        tmp_path,
        repository,
        {'docs/operations.md': edited, 'tests/test_runbook.py': 'def test_it() -> None: ...\n'},
        extra='The gate demands a test, and no owned path holds one: `tests/test_runbook.py` is new.',
    )

    assert _findings(report, Check.OWNED) == []
    assert (
        Check.OWNED,
        'tests/test_runbook.py is a new file outside the owned paths, which the description names',
    ) in (report.notes)


def test_an_undisclosed_new_file_is_found(tmp_path: Path, repository: Path) -> None:
    report = _gates(tmp_path, repository, {'tests/test_runbook.py': 'def test_it() -> None: ...\n'})

    assert _findings(report, Check.OWNED) == [
        'tests/test_runbook.py is a new file outside the owned paths, and the description does not name it '
        '(dispatch.md §1.1 item 5)'
    ]


def test_a_host_name_or_address_the_tree_does_not_hold_is_found(tmp_path: Path, repository: Path) -> None:
    # `github.com` is in a file of the tree and `operations.md` in a path of
    # it; the printer's name and address are in neither.
    report = _gates(
        tmp_path,
        repository,
        {'docs/operations.md': OPERATIONS.replace('What step 3.5 does.', 'What step 3.5 does now.')},
        owned='[docs/]',
        extra=(
            'See https://github.com/Aetf/kluster and `docs/operations.md`; '
            'the printer is printer3.lan at 192.168.40.17 or fd12:3456::17.'
        ),
    )

    assert _findings(report, Check.HOSTS) == [
        f'{name} is named in the description and occurs nowhere in the tree at the head (dispatch.md §1.1 item 6)'
        for name in ('printer3.lan', '192.168.40.17', 'fd12:3456::17')
    ]


@pytest.mark.parametrize(
    ('sweep', 'found'),
    [
        # `\W{0,N}` cannot cross a word, so this control never fires it: the
        # pattern's silence at the head says nothing (style/README.md).
        (
            '- `step 3\\.5\\W{0,40}does`: control: "step 3.5 then does". Over 4 files.',
            ['`step 3\\.5\\W{0,40}does` does not fire on its control "step 3.5 then does"'],
        ),
        (
            '- `step 3\\.5 forgets`: nothing. Over 4 files.',
            [
                '`step 3\\.5 forgets` finds nothing at the head and names no control it fires on, so its silence '
                'is no evidence (style/README.md, "Comments and docs")'
            ],
        ),
        ('- `step 3\\.5 forgets`: control: “step 3.5 forgets the key”. Over 4 files.', []),
        ('- `what step 3\\.4`: the one sentence of §3.4. Over 4 files.', []),
        (
            'Over 4 files, nothing found.',
            ['`## Sweep` names no pattern as a list item that opens with it in a code span (dispatch.md §1.1)'],
        ),
    ],
    ids=['control-that-does-not-fire', 'silent-without-control', 'silent-with-control', 'pattern-with-hits', 'none'],
)
def test_the_sweep_runs_again_and_a_silent_pattern_needs_a_control_it_fires_on(
    tmp_path: Path, repository: Path, sweep: str, found: list[str]
) -> None:
    report = _gates(tmp_path, repository, {'docs/operations.md': OPERATIONS + '\n'}, sweep=sweep)

    assert _findings(report, Check.SWEEP) == found


def test_the_sweep_folds_the_lines_it_reads(tmp_path: Path, repository: Path) -> None:
    # The claim is wrapped across a line break at the head; read line by line
    # the pattern finds nothing, and with no control that would be a finding.
    wrapped = OPERATIONS.replace('What step 3.5 does.', 'What step 3.5\ndoes.')
    report = _gates(tmp_path, repository, {'docs/operations.md': wrapped}, sweep='- `step 3\\.5 does`. Over 4 files.')

    assert _findings(report, Check.SWEEP) == []
    assert (Check.SWEEP, '`step 3\\.5 does`: 1 file') in report.notes


def test_a_file_the_change_deletes_is_no_host_name(tmp_path: Path, repository: Path) -> None:
    # `bring-up.md` reads as a dotted name, and after the deletion only the
    # base's paths hold it.
    report = _gates(
        tmp_path,
        repository,
        {'docs/physical/bring-up.md': None},
        owned='[docs/]',
        extra='`bring-up.md` moves into the runbook.',
    )

    assert _findings(report, Check.HOSTS) == []
