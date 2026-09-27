"""Every job that runs steps names its runner image by release and carries a time bound.

Both are rules of framework/ci.md §3. A runner label that floats is a breaking
upgrade that lands without a pull request, the way an unpinned tool would; a
job with no bound holds its runner, and a joining job its stack's `zt-<stack>`
group, for GitHub's six-hour default. Written as a definition -- every job in
every workflow that runs steps -- so a job added later is held to both without
anyone remembering to list it. A job that calls a reusable workflow runs no
step and names no runner of its own; the called workflow's jobs are read here
too, which is what makes that job exempt.

The same section pins what the jobs run besides the runner: every action from
outside this repository by commit, and ZeroTier, which the composite action
installs from its apt repository, by release and by signing key.

The workflow files are read the way the other seams read them, through the
loaders in `workflow_files`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from renovate_text import as_python_spells_it
from workflow_files import GITHUB, github_name, mapping, read_workflow, workflow_jobs, workflows_and_actions

ROOT = Path(__file__).parent.parent

#: A runner label that names a release: an image name and a version that starts
#: with a digit, with any suffix after it (`ubuntu-24.04`, `ubuntu-24.04-arm`).
#: This is also the shape renovate's github-actions manager reads a label as a
#: versioned runner in; a `-latest` label has no version, and nothing bumps it.
PINNED_RUNNER = re.compile(r'^[a-z]+-\d[\w.]*(?:-[a-z0-9]+)*$')
#: A `runs-on:` that a matrix feeds, as the whole expression.
MATRIX_RUNNER = re.compile(r'^\$\{\{\s*matrix\.([\w-]+(?:\.[\w-]+)*)\s*\}\}$')
#: GitHub's own ceiling for a job on a hosted runner; a larger bound is clamped
#: to it, so writing one would state a bound that does not hold.
HOSTED_JOB_LIMIT_MINUTES = 360

#: The custom manager in `renovate.json5` that reads a matrix-fed runner label,
#: found by the data source it feeds: its one match string, as a JSON5
#: single-quoted string. The github-actions manager reads only a label written
#: in `runs-on:` itself.
RUNNER_MANAGER = re.compile(r"matchStrings: \[\s*'((?:[^'\\]|\\.)*)',\s*\],\s*datasourceTemplate: 'github-runners'")
#: The package rule that groups every runner label, found by its matcher.
RUNNER_RULE = re.compile(r"matchDatasources: \[\s*'github-runners',\s*\]")


def _step_running_jobs() -> dict[str, dict[str, object]]:
    """Every job of every workflow that runs steps, as `<file>: <job>`."""
    return {
        f'{github_name(path)}: {name}': job
        for path in workflows_and_actions()
        if path.parent.name == 'workflows'
        for name, job in workflow_jobs(read_workflow(path), github_name(path)).items()
        if 'uses' not in job
    }


def _matrix_values(job: dict[str, object], where: str, reference: str) -> list[object]:
    """What `matrix.<reference>` takes across a job's matrix, axes and `include` entries alike.

    A matrix written as an expression (`fromJSON(...)`) has no values to read
    here, and neither has a reference that no entry carries; both fail, since
    a label nothing can read is not one anyone has pinned.
    """
    strategy = mapping(job.get('strategy'), f'{where} strategy:')
    matrix = strategy.get('matrix')
    assert isinstance(matrix, dict), f'{where}: runs-on reads a matrix that is not written out'
    matrix = cast('dict[str, object]', matrix)
    axis, *path = reference.split('.')
    rows: list[object] = []
    if isinstance(values := matrix.get(axis), list):
        rows.extend(cast('list[object]', values))
    for entry in cast('list[object]', matrix.get('include', [])):
        included = mapping(entry, f'{where} include entry')
        if axis in included:
            rows.append(included[axis])
    found: list[object] = []
    for row in rows:
        for key in path:
            row = mapping(row, f'{where} matrix.{axis}').get(key)
        found.append(row)
    assert found, f'{where}: no matrix entry carries matrix.{reference}'
    return found


def _runner_labels(job: dict[str, object], where: str) -> tuple[list[object], bool]:
    """The labels a job can run on, and whether a matrix fed them."""
    runs_on = job.get('runs-on')
    labels = cast('list[object]', runs_on) if isinstance(runs_on, list) else [runs_on]
    resolved: list[object] = []
    from_matrix = False
    for label in labels:
        if isinstance(label, str) and (reference := MATRIX_RUNNER.match(label)):
            resolved.extend(_matrix_values(job, where, reference.group(1)))
            from_matrix = True
        else:
            resolved.append(label)
    return resolved, from_matrix


def test_every_job_that_runs_steps_has_a_time_bound() -> None:
    jobs = _step_running_jobs()

    # A read that stopped finding jobs would pass the loop below on nothing.
    assert jobs, 'no workflow job that runs steps was found'
    assert 'workflows/deploy.yml: up-physical' in jobs

    findings: list[str] = []
    for where, job in sorted(jobs.items()):
        bound = job.get('timeout-minutes')
        if bound is None:
            findings.append(f'{where} has no timeout-minutes')
        elif not isinstance(bound, int) or isinstance(bound, bool):
            findings.append(f'{where}: timeout-minutes is {bound!r}, not a number of minutes')
        elif not 0 < bound <= HOSTED_JOB_LIMIT_MINUTES:
            findings.append(f'{where}: timeout-minutes {bound} is outside 1..{HOSTED_JOB_LIMIT_MINUTES}')

    assert findings == [], findings


def test_every_job_that_runs_steps_names_its_runner_by_release() -> None:
    jobs = _step_running_jobs()
    assert jobs, 'no workflow job that runs steps was found'

    findings: list[str] = []
    fed_by_a_matrix: list[str] = []
    for where, job in sorted(jobs.items()):
        labels, from_matrix = _runner_labels(job, where)
        if from_matrix:
            fed_by_a_matrix.append(where)
        findings.extend(
            f'{where} runs on {label!r}, which names no release'
            for label in labels
            if not (isinstance(label, str) and PINNED_RUNNER.match(label))
        )

    # The matrix half of the read is exercised, not only written: the image
    # builds pick their runner per architecture.
    assert 'workflows/images.yml: build' in fed_by_a_matrix
    assert findings == [], findings


def test_renovate_reads_every_runner_label_a_matrix_names() -> None:
    """A matrix-fed label is one the github-actions manager never sees.

    That manager reads a label written in `runs-on:` and skips an expression,
    so a label reached through `${{ matrix... }}` would stay on its release
    while every other job moved. The custom manager in `renovate.json5` reads
    the `runner:` key those labels are written under; every label a matrix
    feeds has to be one its pattern captures, in the file it is written in.
    Renovate reads its configuration from the default branch, so nothing on a
    pull request would go red for a label it missed.
    """
    config = (ROOT / 'renovate.json5').read_text()
    manager = RUNNER_MANAGER.search(config)
    assert manager, 'renovate.json5 has no custom manager feeding the github-runners data source'
    # JSON5 escapes a backslash as two; Python spells a named group `(?P<`.
    pattern = re.compile(manager.group(1).replace('\\\\', '\\').replace('(?<', '(?P<'))

    fed: dict[str, list[str]] = {}
    for where, job in _step_running_jobs().items():
        labels, from_matrix = _runner_labels(job, where)
        if from_matrix:
            fed.setdefault(where.split(': ')[0], []).extend(str(label) for label in labels)
    assert fed, 'no job reads its runner from a matrix'

    missed: list[str] = []
    for name, labels in sorted(fed.items()):
        text = (GITHUB / name).read_text()
        captured = {f'{found["depName"]}-{found["currentValue"]}' for found in pattern.finditer(text)}
        missed.extend(f'{name}: {label}' for label in labels if label not in captured)
    assert missed == [], f'renovate reads no matrix-fed label here: {missed}'


def test_the_runner_images_rule_outranks_the_github_actions_group() -> None:
    """Order is what keeps the labels written in `runs-on:` in the runner-images group.

    The github-actions manager reads those labels, and the rule matching that
    manager puts every dependency it reads in the `github actions` group;
    `packageRules` apply in order and the last match wins a field, so the rule
    matching the `github-runners` data source has to come after it. Above it,
    those labels go back to the actions group while the matrix-fed ones stay in
    their own, which is two pull requests moving one runner image, and nothing
    on a pull request goes red for it: renovate reads its configuration from
    the default branch.
    """
    config = (ROOT / 'renovate.json5').read_text()

    assert config.count("'github-actions'") == 1
    (rule,) = RUNNER_RULE.finditer(config)
    assert rule.start() > config.index("'github-actions'"), 'the runner-images rule must follow the github-actions rule'


#: How an action from outside this repository is referenced (framework/ci.md
#: §3): by the commit, with the release it resolves to as a trailing comment.
#: The comment is where renovate's pin-digest preset reads the release from,
#: and it rewrites the two together.
PINNED_ACTION = re.compile(r'[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40} # v\S+')
#: A `uses:` naming a path in this repository, which is pinned by being here.
LOCAL_ACTION = re.compile(r'[\'"]?(?:\./|\$/)')
#: Every line that sets a `uses:` key, with what follows it; a comment line
#: starts with `#` and is not one.
USES_LINE = re.compile(r'^[ \t-]*uses:[ \t]*(.*?)[ \t]*$', re.MULTILINE)


def _parsed_uses(path: Path) -> list[object]:
    """Every `uses:` value a workflow's jobs and steps, or an action's steps, set."""
    where = github_name(path)
    document = read_workflow(path)
    if path.parent.name == 'workflows':
        jobs = list(workflow_jobs(document, where).values())
    else:
        jobs = [mapping(document.get('runs'), f'{where} runs:')]
    found: list[object] = []
    for job in jobs:
        if 'uses' in job:
            found.append(job['uses'])
        steps = cast('list[object]', job.get('steps', []))
        found.extend(step['uses'] for step in (mapping(s, f'{where} step') for s in steps) if 'uses' in step)
    return found


def test_every_action_from_outside_this_repository_is_pinned_by_commit() -> None:
    """A tag is a pointer its publisher can move, and a moved tag runs in a job holding secrets.

    A step owns the runner for every step after it, so a pin only on the step
    that touches a key is the wrong shape: the rule is every `uses:` that is
    not a path in this repository, in the workflows and in the composite
    actions alike. The comment has to be read off the file's text, which the
    YAML read drops, so the two reads are held to the same count: a `uses:`
    written in a form the line read misses fails that count instead of
    escaping it.
    """
    findings: list[str] = []
    external = 0
    for path in workflows_and_actions():
        written = [found.group(1) for found in USES_LINE.finditer(path.read_text())]
        parsed = _parsed_uses(path)
        if len(written) != len(parsed):
            findings.append(f'{github_name(path)}: {len(parsed)} uses: keys, {len(written)} read off its lines')
        for value in written:
            if LOCAL_ACTION.match(value):
                continue
            external += 1
            if not PINNED_ACTION.fullmatch(value):
                findings.append(f'{github_name(path)}: uses: {value}')

    # A read that stopped finding them would pass the loop above on nothing.
    assert external, 'no action from outside this repository was found'
    assert findings == [], f'not pinned as `<action>@<commit> # v<release>`: {findings}'


ZEROTIER = GITHUB / 'actions' / 'zerotier' / 'action.yml'
#: The oldest ZeroTier release whose rule engine the flow rules confining the
#: CI member are written for (physical/gateway.md §2.3).
ZEROTIER_FLOOR = (1, 14)
#: The custom manager's pattern for the release, as renovate.json5 holds it:
#: renovate's regex engine spells a named group `(?<...>`.
ZEROTIER_MATCH_STRING = "ZEROTIER_VERSION: '(?<currentValue>[^']+)'"


def _zerotier_install() -> tuple[dict[str, str], str]:
    """The composite action's install step: its environment and its script."""
    runs = mapping(read_workflow(ZEROTIER).get('runs'), 'zerotier runs:')
    steps = [mapping(step, 'zerotier step') for step in cast('list[object]', runs.get('steps'))]
    (install,) = (step for step in steps if step.get('name') == 'Install ZeroTier')
    env = mapping(install.get('env'), 'the ZeroTier install env:')
    return {key: str(value) for key, value in env.items()}, str(install.get('run'))


def test_zerotier_is_a_pinned_release_signed_by_a_pinned_key() -> None:
    """What the CI member runs moves only by a pull request, whatever happens on ZeroTier's host.

    The release is the package apt installs, and the fingerprint is the key
    apt is told to trust for the repository: the script exports only the key
    with that fingerprint into the keyring the source names, and refuses when
    what it exported is not that key. A release below the floor would install
    and join, and then the flow rules would not do what they say.
    """
    env, run = _zerotier_install()

    version = env['ZEROTIER_VERSION']
    assert re.fullmatch(r'\d+\.\d+\.\d+(?:-\d+)?', version), f'ZeroTier is not pinned to a release: {version!r}'
    assert tuple(int(part) for part in version.split('.')[:2]) >= ZEROTIER_FLOOR
    assert '"zerotier-one=${ZEROTIER_VERSION}"' in run

    assert re.fullmatch(r'[0-9A-F]{40}', env.get('ZEROTIER_KEY_FINGERPRINT', '')), (
        'no signing-key fingerprint is pinned'
    )
    assert 'gpg --batch --export "$ZEROTIER_KEY_FINGERPRINT" >"$GNUPGHOME/zerotier.gpg"' in run
    assert '[ "$found" != "$ZEROTIER_KEY_FINGERPRINT" ]' in run
    assert '"$GNUPGHOME/zerotier.gpg" /usr/share/keyrings/zerotier.gpg' in run
    assert 'deb [signed-by=/usr/share/keyrings/zerotier.gpg] ' in run


def test_renovate_reads_the_zerotier_release_for_the_suite_the_action_installs_from() -> None:
    """Renovate reads the release off the package index of the suite apt installs it from.

    Its registry URL is written in `renovate.json5` and the suite in the
    action, two spellings of one repository: apart, renovate would offer a
    release the runner's source does not carry. Renovate reads its
    configuration from the default branch, so nothing on a pull request would
    go red for a pattern that stopped matching.
    """
    env, run = _zerotier_install()
    config = (ROOT / 'renovate.json5').read_text()
    pattern = json.dumps(ZEROTIER_MATCH_STRING)

    assert config.count(pattern) == 1
    at = config.index(pattern)
    entry = config[config.rindex('{', 0, at) : config.index('}', at)]
    assert "datasourceTemplate: 'deb'" in entry

    files = re.findall(r"managerFilePatterns: \[\s*'/((?:[^'\\]|\\.)*)/',\s*\]", entry)
    assert len(files) == 1
    assert re.fullmatch(files[0].replace('\\\\', '\\'), str(ZEROTIER.relative_to(ROOT)))
    found = as_python_spells_it(ZEROTIER_MATCH_STRING).search(ZEROTIER.read_text())
    assert found is not None
    assert found['currentValue'] == env['ZEROTIER_VERSION']

    registry = re.search(r"registryUrlTemplate: '([^']*)'", entry)
    assert registry is not None
    url = urlsplit(registry[1])
    suite = env['ZEROTIER_SUITE']
    assert parse_qs(url.query)['suite'] == [suite]
    # The apt source the script writes: the repository, then its suite.
    source = re.search(r'\] (\S+) \$\{ZEROTIER_SUITE\} main', run)
    assert source is not None, 'the install writes no apt source for the suite the action names'
    assert source[1].replace('${ZEROTIER_SUITE}', suite) == f'{url.scheme}://{url.netloc}{url.path}'
