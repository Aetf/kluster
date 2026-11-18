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
installs from its apt repository, by release and by signing key. And every
job that joins the overlay is held to the wait its identity calls for: a
`physical` join waits for every shell it dials, a `dns` join for the first
resolver API to answer. Which jobs join, and the lock they take, are held as
relations (framework/ci.md §2): a joining job enters its identity's one
job-level group, which no other identity's join enters; every entry of such a
group declares `queue: max`; no job a pull request runs both runs `pulumi`
and joins; and the one that joins, the join proof, runs on every change to the
join action or to a workflow that holds a join.

And the merge chain applies in the order that section gives it: `up-k8s-base`
and `up-apps` follow `up-dns`, each on a gate of its own, whatever `up-dns`
returned. The conditions are read by a model of how GitHub evaluates a job's
`if:` and evaluated over every result the jobs up the chain can end in, so a
condition written another way is held to what it does rather than to how it
is spelled.

And every refresh of `physical` that a workflow or a composite action runs
puts the program first (physical/homelab-host.md §6), read off the `pulumi`
commands the steps run in each matrix combination rather than off one step.

And the plan behind `physical`'s approval gate is read as three outcomes,
held by running the plan step against a stand-in for what it runs: an `up`
that would write nothing, one that would write, and no answer, which fails
the job rather than reading as a diff. The step routes to the gated apply on
what an `up` would write rather than on the steps a preview counts, held by
running it as written against the pinned CLI over a scratch stack named
`physical`: a `protect` set with no other change (Aetf/kluster#753) reads
as a diff. Every job that runs the program installs its plugins first,
authenticated with the job's token, and that token reaches no step that runs
the program, a console script that runs a `pulumi` subcommand of the program
read as that subcommand.

And every `Up` step of the merge chain hands `release-lock` the stack it
applied, the pid its `pulumi` ran as and a time no later than its start, once
that `pulumi` has exited by itself, and never after a signal it does not trap
ended it (framework/ci.md §3.3), held by running the step against stand-ins
for `mise`, `pulumi` and `uv`. And every `Up` step that applies a stack whose
objects start pods on the control planes runs `pulumi up` at the parallelism
the workflow's own `env` holds (cluster/nodes.md §1.2), held the same way.

The workflow files are read the way the other seams read them, through the
loaders in `workflow_files`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from itertools import product
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import process_sessions
import pytest
import yaml
from renovate_text import as_python_spells_it
from scratch_projects import scratch_project
from workflow_files import (
    GITHUB,
    github_name,
    mapping,
    read_workflow,
    workflow_jobs,
    workflow_on,
    workflow_triggers,
    workflows_and_actions,
)

from kluster import conventions
from kluster.components.overlay.flow_rules import SSH_PORT
from kluster.lib import stack_environment
from kluster.scripts.credentials import slots
from kluster.scripts.stack_plan import cli as stack_plan_cli

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

    # The matrix half of the read is exercised, not only written: a job
    # picks its runner from a matrix.
    assert fed_by_a_matrix, 'no job reads its runner from a matrix'
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
#: The custom manager in `renovate.json5` that reads the ZeroTier release,
#: found by the data source it feeds, the one `deb` source there is: its one
#: match string, as a JSON5 double-quoted string.
ZEROTIER_MANAGER = re.compile(
    r'matchStrings: \[\s*"((?:[^"\\]|\\.)*)",\s*\],\s*depNameTemplate: \'[^\']*\',\s*datasourceTemplate: \'deb\''
)


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
    what it exported is not that key.
    """
    env, run = _zerotier_install()

    version = env['ZEROTIER_VERSION']
    assert re.fullmatch(r'\d+\.\d+\.\d+(?:-\d+)?', version), f'ZeroTier is not pinned to a release: {version!r}'
    assert '"zerotier-one=${ZEROTIER_VERSION}"' in run

    assert re.fullmatch(r'[0-9A-F]{40}', env.get('ZEROTIER_KEY_FINGERPRINT', '')), (
        'no signing-key fingerprint is pinned'
    )
    assert 'gpg --batch --export "$ZEROTIER_KEY_FINGERPRINT" >"$GNUPGHOME/zerotier.gpg"' in run
    assert '[ "$found" != "$ZEROTIER_KEY_FINGERPRINT" ]' in run
    assert '"$GNUPGHOME/zerotier.gpg" /usr/share/keyrings/zerotier.gpg' in run
    assert 'deb [signed-by=/usr/share/keyrings/zerotier.gpg] ' in run


#: The record of the vendored ZeroTier rule compiler `test_flow_rules_compile`
#: runs over the overlay's flow rules.
RULE_COMPILER = Path(__file__).parent / 'vendor' / 'zerotier-rule-compiler' / 'source.toml'


def test_the_rule_compiler_is_the_release_the_ci_member_runs() -> None:
    """The flow rules are compiled by the release this installation runs, and move with it.

    Renovate moves the action's release and nothing else; the vendored
    compiler is taken again by hand, as `source.toml` says. So the pull request
    that moves the release is red here until the compiler it vendors is the
    same release's.
    """
    env, _ = _zerotier_install()
    vendored = tomllib.loads(RULE_COMPILER.read_text())['version']

    assert vendored == env['ZEROTIER_VERSION'], (
        f'the vendored rule compiler is {vendored} and the CI member installs {env["ZEROTIER_VERSION"]}; '
        f'take it again from the new tag as {RULE_COMPILER.relative_to(ROOT)} says'
    )


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

    (manager,) = ZEROTIER_MANAGER.finditer(config)
    # JSON5 escapes a backslash as two.
    match_string = manager.group(1).replace('\\\\', '\\')
    entry = config[config.rindex('{', 0, manager.start()) : config.index('}', manager.end())]

    files = re.findall(r"managerFilePatterns: \[\s*'/((?:[^'\\]|\\.)*)/',\s*\]", entry)
    assert len(files) == 1
    assert re.fullmatch(files[0].replace('\\\\', '\\'), str(ZEROTIER.relative_to(ROOT)))
    found = as_python_spells_it(match_string).search(ZEROTIER.read_text())
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


#: How a step names the composite action that joins the overlay.
JOIN_ACTION = './.github/actions/zerotier'
#: A value that is a matrix property and nothing else.
MATRIX_VALUE = re.compile(r'^\$\{\{\s*matrix\.([\w-]+)\s*\}\}$')
#: The two step conditions a join is written under: a matrix property compared
#: with a literal, and a matrix property read as a flag.
MATRIX_EQUALS = re.compile(r"^matrix\.([\w-]+) == '([^']*)'$")
MATRIX_FLAG = re.compile(r'^matrix\.([\w-]+)$')


#: An expression anywhere in a value.
EXPRESSION = re.compile(r'\$\{\{.*?\}\}')


@dataclass(frozen=True)
class Concurrency:
    """A `concurrency` block as one matrix combination makes it.

    `group` is the group's name as GitHub compares it, without regard to
    case, and empty for a block that is absent. A part of it this read cannot
    evaluate -- an expression other than a matrix property the combination
    carries -- stays in it as written and stands for any text (`enters`).
    `cancels` is whether the block may cancel what is in progress, an
    expression counting as one that may. `queue` is the `queue` value as
    written, empty where the block leaves it out.
    """

    group: str
    cancels: bool
    queue: str

    @property
    def literal(self) -> bool:
        """Whether the group's name is known whole."""
        return '${{' not in self.group

    def enters(self, group: str) -> bool:
        """Whether this block's group can be `group`, an unread expression matching any text."""
        if not self.group:
            return False
        pattern = ''.join(
            '.*' if EXPRESSION.fullmatch(part) else re.escape(part) for part in re.split(r'(\$\{\{.*?\}\})', self.group)
        )
        return re.fullmatch(pattern, group.casefold()) is not None


def _concurrency(block: object, row: dict[str, object] | None) -> Concurrency:
    """A `concurrency` block, workflow- or job-level, in one matrix combination, in either shape GitHub takes it.

    `row` is `None` where the combination cannot be read, which leaves every
    matrix property unread.
    """
    if block is None:
        return Concurrency('', cancels=False, queue='')
    if not isinstance(block, dict):
        block = {'group': block}
    block = cast('dict[str, object]', block)

    def matrix(reference: re.Match[str]) -> str:
        if row is None or reference.group(1) not in row:
            return reference.group(0)
        return str(row[reference.group(1)])

    group = MATRIX_REFERENCE.sub(matrix, str(block.get('group', ''))).casefold()
    cancels = block.get('cancel-in-progress', False)
    return Concurrency(group, cancels=cancels is not False, queue=str(block.get('queue', '')))


@dataclass(frozen=True)
class Join:
    """One way a job can join the overlay: where, in which Environment, what it routes, and what it waits for.

    `wait_for` holds the targets every one of which has to answer, and
    `wait_for_any` the targets the first answer among which ends their wait.
    """

    where: str
    environment: str
    routes: str
    wait_for: str
    wait_for_any: str
    #: The job-level concurrency group the job enters in this combination.
    concurrency: Concurrency


def _matrix_rows(job: dict[str, object], where: str) -> list[dict[str, object]] | None:
    """Every combination a job's matrix runs, or `None` for a matrix with an axis written as an expression.

    Axes and `include` entries are read, but not both in one matrix: GitHub
    merges an `include` entry into the combinations it matches, which no join
    here relies on, so a matrix that mixes them is refused rather than read
    wrongly.
    """
    strategy = job.get('strategy')
    if strategy is None:
        return [{}]
    matrix = mapping(strategy, f'{where} strategy:').get('matrix')
    if not isinstance(matrix, dict):
        return None
    matrix = cast('dict[str, object]', matrix)
    include = [mapping(entry, f'{where} include entry') for entry in cast('list[object]', matrix.get('include', []))]
    axes = {key: values for key, values in matrix.items() if key not in {'include', 'exclude'}}
    # An axis written as an expression (`fromJSON(...)`) has no values to read.
    if not all(isinstance(values, list) for values in axes.values()):
        return None
    assert not (axes and include), f'{where}: a matrix with both axes and include entries is not read here'
    if include:
        return include
    rows: list[dict[str, object]] = [{}]
    for key, values in axes.items():
        rows = [row | {key: value} for row in rows for value in cast('list[object]', values)]
    return rows


def _resolved(value: object, row: dict[str, object], where: str) -> str:
    """A step or job value as one matrix combination makes it."""
    text = '' if value is None else str(value)
    if reference := MATRIX_VALUE.match(text):
        assert reference.group(1) in row, f'{where}: matrix.{reference.group(1)} is not in {row}'
        return str(row[reference.group(1)])
    assert '${{' not in text, f'{where}: {text!r} is an expression this case does not read'
    return text


def _joins() -> list[Join]:
    """Every join any workflow can make, one per matrix combination its step runs in."""
    joins: list[Join] = []
    for where, job in sorted(_step_running_jobs().items()):
        steps = [mapping(step, f'{where} step') for step in cast('list[object]', job.get('steps', []))]
        for step in steps:
            if step.get('uses') != JOIN_ACTION:
                continue
            rows = _matrix_rows(job, where)
            condition = step.get('if')
            if isinstance(condition, str) and (equals := MATRIX_EQUALS.match(condition)):
                key, value = equals.groups()
                only: dict[str, object] = {key: value}
                rows = [only] if rows is None else [row for row in rows if str(row.get(key)) == value]
            elif isinstance(condition, str) and (flag := MATRIX_FLAG.match(condition)):
                assert rows is not None, f'{where}: a join under {condition!r} over a matrix this case cannot read'
                rows = [row for row in rows if row.get(flag.group(1)) is True]
            else:
                assert condition is None, f'{where}: a join under {condition!r}, a condition this case does not read'
                assert rows is not None, f'{where}: a join over a matrix this case cannot read'
            # A join no combination runs would leave the step unread.
            assert rows, f'{where}: no matrix combination runs its join under {condition!r}'
            environment = job.get('environment')
            if isinstance(environment, dict):
                environment = cast('dict[str, object]', environment).get('name')
            inputs = mapping(step.get('with', {}), f'{where} join with:')
            joins.extend(
                Join(
                    where,
                    _resolved(environment, row, where),
                    _resolved(inputs.get('routes'), row, where),
                    _resolved(inputs.get('wait_for'), row, where),
                    _resolved(inputs.get('wait_for_any'), row, where),
                    _concurrency(job.get('concurrency'), row),
                )
                for row in rows
            )
    return joins


def _routes(text: str) -> set[tuple[IPv4Network, IPv4Address]]:
    """A join's `routes` input as the action reads it: one `<target> via <next hop>` per line."""
    routes: set[tuple[IPv4Network, IPv4Address]] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        target, word, hop = line.split()
        assert word == 'via', line
        routes.add((IPv4Network(target), IPv4Address(hop)))
    return routes


def test_a_physical_join_waits_for_every_shell_it_dials_and_a_dns_join_routes_to_and_waits_for_the_resolvers() -> None:
    """Which identity a job joins with decides what its join routes and waits on.

    The identity is the Environment's secret, so the Environment decides it
    (`slots.ZEROTIER_PHYSICAL`, `slots.ZEROTIER_DNS`). A `physical` run dials
    two members over the overlay: the gateway's shell, which the device-files
    push connects to, and the homelab host's, which the libvirt provider
    connects to. The flow rules can drop a fresh member's first packets to
    each peer (physical/gateway.md §2.3), so its join waits for both shells,
    every one of them; the gateway's readiness line is where §2.4 there reads
    items 4 and 7. It dials members only, and installs no route.

    A `dns` run dials the resolvers' APIs at their container-VLAN addresses,
    which the network carries no route to: a route only one run needs is that
    run's own (framework/ci.md §2). So its join installs a host route to each
    resolver the census lists, via the gateway, and nothing wider -- a route
    the census lacks would be one the flow rules drop anyway, and a resolver
    left out one the run cannot reach -- and waits for those same APIs, any
    one answering. Held against the resolver census, the API port, and the
    gateway's and the homelab host's overlay addresses, which the workflows
    spell as text no import reaches.
    """
    joins = _joins()
    physical = [join for join in joins if join.environment in slots.ZEROTIER_PHYSICAL]
    dns = [join for join in joins if join.environment in slots.ZEROTIER_DNS]

    # A read that stopped finding either kind would pass on nothing.
    assert physical, 'no job joins with the physical identity'
    assert dns, 'no job joins with the dns identity'
    # An Environment holding neither identity has nothing to join with.
    assert [join for join in joins if join not in physical + dns] == []
    homelab = conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address
    shells = {f'{conventions.overlay.UDM}:{SSH_PORT}', f'{homelab}:{SSH_PORT}'}
    assert [join.where for join in physical if set(join.wait_for.split()) != shells] == []
    assert [join.where for join in physical if join.wait_for_any.strip()] == []
    assert [join for join in physical if join.routes.strip()] == []

    resolvers = [resolver.address for resolver in conventions.gateway.RESOLVERS]
    routes = {(IPv4Network(f'{address}/32'), conventions.overlay.UDM) for address in resolvers}
    apis = {f'{address}:{conventions.gateway.ADGUARD_API_PORT}' for address in resolvers}
    assert [join.where for join in dns if _routes(join.routes) != routes] == []
    assert [join.where for join in dns if set(join.wait_for_any.split()) != apis] == []
    assert [join.where for join in dns if join.wait_for.strip()] == []


def _identity(environment: str) -> tuple[str, ...]:
    """The identity a job in `environment` joins with, as the Environments that carry it."""
    (identity,) = [carriers for carriers in (slots.ZEROTIER_PHYSICAL, slots.ZEROTIER_DNS) if environment in carriers]
    return identity


def test_a_joining_job_takes_its_identitys_one_group_and_no_other_identity_shares_it() -> None:
    """The lock that keeps an identity live in one job at a time is a group named for the identity.

    ZeroTier maps a member to one endpoint at a time, and the jobs that join
    with one identity sit in different workflows, so the lock is a job-level
    group, which is repository-wide (framework/ci.md §2). Held as relations
    rather than names: every combination a join runs in enters a job-level
    group; every join with one identity enters the same one; and no two
    identities share a group, which would serialize one against the other for
    nothing.
    """
    joins = _joins()
    # A read that stopped finding joins would pass on nothing.
    assert joins, 'no job joins the overlay'

    assert [join.where for join in joins if not join.concurrency.group] == []
    # A group this read cannot name could be any identity's.
    assert [join.where for join in joins if not join.concurrency.literal] == []
    groups: dict[tuple[str, ...], set[str]] = {}
    for join in joins:
        groups.setdefault(_identity(join.environment), set()).add(join.concurrency.group)
    assert {identity: entered for identity, entered in groups.items() if len(entered) != 1} == {}
    entered = [group for each in groups.values() for group in each]
    assert len(set(entered)) == len(entered), groups


#: The `queue` value that keeps up to GitHub's ceiling of entries pending in a
#: group rather than one, which a newer entry cancels.
QUEUE_MAX = 'max'


def _group_entries() -> list[tuple[str, Concurrency]]:
    """Every group any workflow or job enters, one per matrix combination where it can be read, with where.

    A workflow's own group is read beside its jobs', and a job that calls a
    reusable workflow beside a job that runs steps: each is an entrant of
    the group it names. A matrix this read cannot expand leaves the matrix
    properties in the group unread.
    """
    entries: list[tuple[str, Concurrency]] = []
    for path in workflows_and_actions():
        if path.parent.name != 'workflows':
            continue
        workflow = read_workflow(path)
        if 'concurrency' in workflow:
            entries.append((github_name(path), _concurrency(workflow['concurrency'], {})))
        for name, job in workflow_jobs(workflow, github_name(path)).items():
            if 'concurrency' not in job:
                continue
            where = f'{github_name(path)}: {name}'
            rows: list[dict[str, object]] | list[None] = _matrix_rows(job, where) or [None]
            entries.extend((where, _concurrency(job['concurrency'], row)) for row in rows)
    return entries


def test_every_group_a_joining_job_enters_queues() -> None:
    """A group delays a joining job and never cancels it.

    With GitHub's default, a group keeps one pending entry and a newer one
    cancels it, and a cancelled job is neither success nor failure: a deploy
    job cancelled that way silently does not apply, and a drift check silently
    does not run (framework/ci.md §2). `queue: max` keeps the pending entries
    in order instead. Held for every entry of every such group, a job that
    does not join included, since one entry that cancels is enough to cancel
    the joining job pending beside it. A workflow's own group is an entrant
    too, group names compare without regard to case, as GitHub compares
    them, and a group this read cannot name counts as an entrant of every
    group it could be.
    """
    joining = {join.concurrency.group for join in _joins()}
    entries = [(where, entry) for where, entry in _group_entries() if any(entry.enters(group) for group in joining)]

    # A read that stopped finding the groups would pass on nothing.
    assert entries, 'no job enters a group a joining job enters'
    failing = [(where, entry) for where, entry in entries if entry.queue != QUEUE_MAX or entry.cancels]
    # A group this read cannot name whole is read as entering each joining
    # group its literal text admits, whatever it evaluates to on the forge.
    assert [where for where, entry in failing if not entry.literal] == [], (
        'a group this read cannot name whole counts as an entrant of each joining group its literal text admits: '
        'give it a literal prefix no joining group shares, or queue it'
    )
    assert [where for where, _ in failing] == []


#: The events that run a workflow for a pull request.
PULL_REQUEST_EVENTS = frozenset({'pull_request', 'pull_request_target'})
#: A job that calls a reusable workflow of this repository.
LOCAL_WORKFLOW = re.compile(r'^\./\.github/workflows/([\w.-]+)$')


def _pull_request_jobs() -> dict[str, dict[str, object]]:
    """Every job that runs steps for a pull request, the jobs of a workflow a pull request's run calls included."""
    workflows = {path.name: read_workflow(path) for path in workflows_and_actions() if path.parent.name == 'workflows'}
    reached = [name for name, workflow in workflows.items() if workflow_triggers(workflow) & PULL_REQUEST_EVENTS]
    for name in reached:
        for job in workflow_jobs(workflows[name], name).values():
            called = LOCAL_WORKFLOW.match(str(job.get('uses', '')))
            if called and called.group(1) not in reached:
                reached.append(called.group(1))
    return {
        f'workflows/{name}: {job_name}': job
        for name in reached
        for job_name, job in workflow_jobs(workflows[name], name).items()
        if 'uses' not in job
    }


def _steps(job: dict[str, object], where: str) -> list[dict[str, object]]:
    return [mapping(step, f'{where} step') for step in cast('list[object]', job.get('steps', []))]


def test_no_job_that_runs_pulumi_for_a_pull_request_joins() -> None:
    """A job joins only when its run dials something over the overlay.

    A pull request's `pulumi` runs are previews without `--refresh` of `dns`,
    `k8s-base` and `apps`. The `dns` preview calls its dynamic providers'
    `configure`, `check` and `diff`, none of which dials
    (`test_dns_preview_dials_nothing`), and the other two reach nothing on
    the LAN. A join there would hold the
    identity's lock, wait on the gateway, and fail the preview when the
    gateway is down, for nothing (framework/ci.md §2). So no job a pull request
    runs both runs `pulumi` and joins.
    """
    jobs = _pull_request_jobs()
    running = {
        where
        for where, job in jobs.items()
        for step in _steps(job, where)
        if isinstance(run := step.get('run'), str) and _pulumi_commands(run, where)
    }
    joining = {where for where, job in jobs.items() for step in _steps(job, where) if step.get('uses') == JOIN_ACTION}

    # A read that stopped finding the previews would pass on nothing.
    assert running, 'no job runs pulumi for a pull request'
    assert running & joining == set()


#: How a job's condition reads a path filter's answer: one output of a job it
#: needs, compared with the string the filter action writes for a match.
FILTER_CONDITION = re.compile(r"^needs\.([\w-]+)\.outputs\.([\w-]+) == 'true'$")
#: How that job's output names the step that answers it.
STEP_OUTPUT = re.compile(r'^\$\{\{\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}$')
#: The action that answers a path filter.
PATHS_FILTER = 'dorny/paths-filter@'


#: A character a path glob is read as itself here: one a path in this repository is written with.
GLOB_LITERAL = re.compile(r'[\w./-]')


def _glob(pattern: str, where: str) -> re.Pattern[str]:
    """A path filter's glob as a pattern over a path from the repository root: `**` crosses directories, `*` does not.

    A character that is neither a literal path character nor `*` is refused by
    name rather than read as itself. GitHub's filter patterns give `?`, `+`,
    `[]` and the escaping `\\` meanings of their own, and the path filter's
    matcher has a syntax of its own besides; read here as a literal, such a
    character leaves unmatched a path the forge matches, which in a `!` glob or
    a `paths-ignore` list keeps a path the forge drops.

    A `**` that is not a whole path segment is refused too. The path filter's
    matcher, `picomatch`, reads one as a plain `*`, which does not cross a
    `/`, so `.github**` there matches nothing under `.github/`, where this
    read's `.*` would match all of it.
    """
    unread = sorted(set(GLOB_LITERAL.sub('', pattern)) - {'*'})
    assert not unread, f'{where}: the glob {pattern!r} uses {"".join(unread)!r}, which this read does not model'
    partial = [segment for segment in pattern.split('/') if '**' in segment and segment != '**']
    assert not partial, (
        f'{where}: the glob {pattern!r} has a `**` in {partial}, not a whole path segment, which this read does not model'
    )
    parts = re.split(r'(\*\*/?|\*)', pattern)
    translated = ''.join(
        '(?:.*/)?' if part == '**/' else '.*' if part == '**' else '[^/]*' if part == '*' else re.escape(part)
        for part in parts
    )
    return re.compile(f'^{translated}$')


def _filter_patterns(workflow: dict[str, object], name: str, condition: object, where: str) -> list[str]:
    """The globs a job's condition reads through a path filter of a job it needs."""
    assert isinstance(condition, str), f'{where}: a join for a pull request runs under no path filter'
    read = FILTER_CONDITION.match(condition.strip())
    assert read, f'{where}: a join for a pull request runs under {condition!r}, a condition this case does not read'
    needed, output = read.groups()
    producer = workflow_jobs(workflow, name)[needed]
    answer = STEP_OUTPUT.match(str(mapping(producer.get('outputs'), f'{where} {needed} outputs:').get(output)))
    assert answer, f'{where}: {needed}.outputs.{output} is not one step output'
    step_id, filter_name = answer.groups()
    (step,) = [step for step in _steps(producer, needed) if step.get('id') == step_id]
    assert str(step.get('uses', '')).startswith(PATHS_FILTER), f'{where}: {needed} step {step_id} is not a path filter'
    inputs = mapping(step.get('with'), f'{where} {needed} step {step_id} with:')
    # Read under the action's default quantifier, `some`: a path matches when
    # any glob does, which only holds for globs that are not negated.
    assert 'predicate-quantifier' not in inputs, f'{where}: the filter sets a quantifier this case does not read'
    filters = mapping(yaml.safe_load(str(inputs.get('filters'))), f'{where} filters')
    patterns = [str(pattern) for pattern in cast('list[object]', filters[filter_name])]
    assert not [pattern for pattern in patterns if pattern.startswith('!')], f'{where}: a negated glob'
    return patterns


def _filter_globs(workflow: dict[str, object], name: str, condition: object, where: str) -> list[re.Pattern[str]]:
    """The globs a job's condition reads through a path filter of a job it needs, each as a pattern."""
    return [_glob(pattern, where) for pattern in _filter_patterns(workflow, name, condition, where)]


def _last_match(patterns: object, path: str, where: str) -> bool:
    """Whether `path` is selected by a trigger's `paths` or `paths-ignore` list.

    GitHub reads the list in order, and the last glob that matches decides: a
    plain glob selects the path and a `!` glob deselects it.
    """
    assert isinstance(patterns, list), f'{where}: a path list that is not a list'
    selected = False
    for pattern in cast('list[object]', patterns):
        text = str(pattern)
        if _glob(text.removeprefix('!'), where).match(path):
            selected = not text.startswith('!')
    return selected


def _trigger_admits(workflow: dict[str, object], path: str, where: str) -> bool:
    """Whether a pull request that changes `path` alone starts the workflow, by the path lists of its triggers."""
    on = workflow_on(workflow)
    events = workflow_triggers(workflow) & PULL_REQUEST_EVENTS
    assert events, f'{where}: the workflow does not run for a pull request'
    for event in sorted(events):
        settings = cast('dict[object, object]', on).get(event) if isinstance(on, dict) else None
        if not isinstance(settings, dict):
            return True
        settings = cast('dict[str, object]', settings)
        if 'paths' in settings and not _last_match(settings['paths'], path, where):
            continue
        if 'paths-ignore' in settings and _last_match(settings['paths-ignore'], path, where):
            continue
        return True
    return False


def _triggered(paths: dict[str, list[str]]) -> Callable[[], object]:
    """A read of whether a workflow whose pull-request trigger carries `paths` runs for a change to the join action."""
    workflow: dict[str, object] = {'on': {'pull_request': paths}}
    return lambda: _trigger_admits(workflow, str(ZEROTIER.relative_to(ROOT)), 'preview.yml')


def _filtered(*globs: str) -> Callable[[], object]:
    """A read of the globs a job's path filter answers its condition with, the filter holding `globs`."""
    step = {'id': 'join', 'uses': f'{PATHS_FILTER}pinned', 'with': {'filters': yaml.safe_dump({'join': list(globs)})}}
    producer = {'outputs': {'join': '${{ steps.join.outputs.join }}'}, 'steps': [step]}
    workflow: dict[str, object] = {'jobs': {'changes': producer}}
    return lambda: _filter_globs(workflow, 'preview.yml', "needs.changes.outputs.join == 'true'", 'preview.yml: join')


@pytest.mark.parametrize(
    ('read', 'glob'),
    [
        (_triggered({'paths-ignore': ['.github/[a]ctions/**']}), '.github/[a]ctions/**'),
        (
            _triggered({'paths': ['**', '!.github/actions/zerotier/action.y?ml']}),
            '.github/actions/zerotier/action.y?ml',
        ),
        (_triggered({'paths-ignore': ['.github/actions+/**']}), '.github/actions+/**'),
        (_filtered('.github**'), '.github**'),
    ],
    ids=['paths-ignore-class', 'negated-question-mark', 'paths-ignore-plus', 'filter-partial-globstar'],
)
def test_a_path_glob_in_syntax_this_read_does_not_model_is_refused_by_name(
    read: Callable[[], object], glob: str
) -> None:
    """A glob the forge reads differently is refused by name, never read as this case would read it.

    On the forge each of these keeps a change to the join action from the
    proof: the trigger's from the workflow's run, the filter's from the job.
    Read as this case reads the rest of a glob -- a character as itself, a
    `**` as crossing directories -- each would leave the join proof's case
    green.
    """
    with pytest.raises(AssertionError, match=re.escape(repr(glob))):
        _ = read()


def test_a_pull_request_that_changes_what_a_join_runs_proves_the_join() -> None:
    """The join is proved before a merge whenever a change could break it.

    A pull request's `pulumi` jobs do not join, so the one job that does is the
    proof: it runs whenever the change touches the join action, which carries
    the pinned release, or a workflow holding a join, which carries the runner
    label and the inputs it is called with (framework/ci.md §2). Held at both
    places a change can be filtered out before the join runs: the path lists
    of the workflow's own pull-request triggers, and the path filter the job's
    condition answers to, each matched against every one of those files.
    """
    workflows = {path.name: read_workflow(path) for path in workflows_and_actions() if path.parent.name == 'workflows'}
    proofs = [
        (where, job)
        for where, job in _pull_request_jobs().items()
        if any(step.get('uses') == JOIN_ACTION for step in _steps(job, where))
    ]
    # `where` names a job as `<file under .github/>: <job>`.
    holding = {GITHUB / join.where.split(': ')[0] for join in _joins()}
    exercised = sorted(str(path.relative_to(ROOT)) for path in {ZEROTIER, *holding})

    assert proofs, 'no job a pull request runs joins the overlay'
    for where, job in proofs:
        name = where.split(': ')[0].removeprefix('workflows/')
        assert [path for path in exercised if not _trigger_admits(workflows[name], path, where)] == [], where
        patterns = _filter_globs(workflows[name], name, job.get('if'), where)
        assert [path for path in exercised if not any(pattern.match(path) for pattern in patterns)] == [], where


#: A token of a job condition, by kind: a string literal, an operator or
#: punctuation, a name. Anything else stops the read, so a condition in a form
#: `_Condition` does not model fails rather than reading as something it is not.
CONDITION_TOKEN = re.compile(r"\s*(?:(?P<string>'[^']*')|(?P<operator>==|&&|\|\||[().])|(?P<name>[A-Za-z_][\w-]*))")
#: Every result a job a condition reads can end in.
JOB_RESULTS: tuple[str, ...] = ('success', 'failure', 'cancelled', 'skipped')

#: A read condition: an operator or kind, then its operands.
type _Node = tuple[object, ...]


class _Condition:
    """A job's `if:`, read as GitHub evaluates it against the results of the jobs up its chain.

    The model covers what the conditions of the jobs it reads are written in:
    string literals, `needs.<job>.result`, `==` (which ignores case, as
    GitHub's does), `&&`, `||`, parentheses and `always()`. A condition naming
    no status function is evaluated as `success() && (<condition>)`, as GitHub
    does, and that `success()` holds when every job up the job's chain
    succeeded, the jobs it needs and every job they depend on in turn: a job
    skipped or failed anywhere above stops it, which is how a skip cascades
    down a chain. A job with no `if:` is that `success()` alone. Anything else
    -- another path under `needs`, another function, another operator or
    literal -- is refused while the condition is read.
    """

    def __init__(self, text: str, needs: list[str], where: str) -> None:
        self.where = where
        self.needs = needs
        self.tokens = self._tokenize(text.strip())
        self.position = 0
        self.always = False
        self.tree: _Node | None = self._or() if self.tokens else None
        assert self.position == len(self.tokens), f'{where}: {text!r} has text past its end'

    def _tokenize(self, text: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        position = 0
        while text[position:].strip():
            found = CONDITION_TOKEN.match(text, position)
            assert found and found.lastgroup, f'{self.where}: {text[position:]!r} is not read here'
            tokens.append((found.lastgroup, found.group(found.lastgroup)))
            position = found.end()
        return tokens

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self, expected: tuple[str, str] | None = None) -> tuple[str, str]:
        token = self._peek()
        assert token is not None, f'{self.where}: the condition ends early'
        assert expected is None or token == expected, f'{self.where}: expected {expected}, not {token}'
        self.position += 1
        return token

    def _binary(self, operator: str, operand: Callable[[], _Node]) -> _Node:
        node = operand()
        while self._peek() == ('operator', operator):
            self._take()
            node = (operator, node, operand())
        return node

    def _or(self) -> _Node:
        return self._binary('||', self._and)

    def _and(self) -> _Node:
        return self._binary('&&', self._comparison)

    def _comparison(self) -> _Node:
        return self._binary('==', self._primary)

    def _primary(self) -> _Node:
        kind, text = self._take()
        if (kind, text) == ('operator', '('):
            node = self._or()
            self._take(('operator', ')'))
            return node
        if kind == 'string':
            return ('literal', text[1:-1])
        assert kind == 'name', f'{self.where}: {text!r} cannot start an operand'
        if text == 'always':
            self._take(('operator', '('))
            self._take(('operator', ')'))
            self.always = True
            return ('always',)
        assert text == 'needs', f'{self.where}: {text!r} is not modeled here'
        self._take(('operator', '.'))
        _, job = self._take()
        self._take(('operator', '.'))
        assert self._take() == ('name', 'result'), f'{self.where}: only needs.<job>.result is modeled here'
        assert job in self.needs, f'{self.where}: reads needs.{job}, a job it does not need'
        return ('result', job)

    def runs(self, results: Mapping[str, str]) -> bool:
        """Whether the job runs when every job up its chain ended as `results` says."""
        value = True if self.tree is None else self._evaluate(self.tree, results)
        assert isinstance(value, bool), f'{self.where}: the condition is not a comparison'
        if self.always:
            return value
        return all(result == 'success' for result in results.values()) and value

    def _evaluate(self, node: _Node, results: Mapping[str, str]) -> str | bool:
        match node:
            case ('literal', str() as value):
                return value
            case ('always',):
                return True
            case ('result', str() as job):
                return results[job]
            case ('==', left, right):
                lhs = self._evaluate(cast('_Node', left), results)
                rhs = self._evaluate(cast('_Node', right), results)
                assert isinstance(lhs, str) and isinstance(rhs, str), f'{self.where}: == between non-strings'
                return lhs.casefold() == rhs.casefold()
            case (('&&' | '||') as operator, left, right):
                lhs = self._evaluate(cast('_Node', left), results)
                rhs = self._evaluate(cast('_Node', right), results)
                assert isinstance(lhs, bool) and isinstance(rhs, bool), f'{self.where}: {operator} between non-booleans'
                return (lhs and rhs) if operator == '&&' else (lhs or rhs)
            case _:
                raise AssertionError(f'{self.where}: unread node {node!r}')


DEPLOY = GITHUB / 'workflows' / 'deploy.yml'


def _needs(job: dict[str, object]) -> list[str]:
    """The jobs a job names in `needs:`, written as one name or a list."""
    needs = job.get('needs', [])
    return [needs] if isinstance(needs, str) else [str(need) for need in cast('list[object]', needs)]


def _deploy_job(name: str) -> tuple[list[str], list[str], _Condition]:
    """A `deploy.yml` job's needs, every job up its chain, and its condition read against them."""
    jobs = workflow_jobs(read_workflow(DEPLOY), github_name(DEPLOY))
    needs = _needs(jobs[name])
    chain: list[str] = []
    pending = list(needs)
    while pending:
        if (above := pending.pop()) not in chain:
            chain.append(above)
            pending.extend(_needs(jobs[above]))
    condition = jobs[name].get('if')
    where = f'{github_name(DEPLOY)}: {name}'
    return needs, sorted(chain), _Condition('' if condition is None else str(condition), needs, where)


def _physical_gate_passed(results: Mapping[str, str]) -> bool:
    """The physical gate (framework/ci.md §3): planned, and applied or found to need no apply."""
    return results['plan-physical'] == 'success' and results['up-physical'] in {'success', 'skipped'}


#: The stacks that follow `up-dns`, each with the gate of its own it runs
#: behind: `k8s-base` the physical gate, `apps` `k8s-base`'s success.
AFTER_DNS: dict[str, Callable[[Mapping[str, str]], bool]] = {
    'up-k8s-base': _physical_gate_passed,
    'up-apps': lambda results: results['up-k8s-base'] == 'success',
}


def test_the_stacks_a_cut_hands_a_name_to_wait_for_dns() -> None:
    """A cut deletes a name's legacy record in `dns` and creates its record in `k8s-base` or `apps`.

    Cloudflare refuses the second record while the first stands, and neither
    stack can adopt the other's, so each of the two runs only once `up-dns`
    has ended (framework/ci.md §3).
    """
    missing = [job for job in AFTER_DNS if 'up-dns' not in _deploy_job(job)[0]]

    assert missing == [], f'these jobs do not need up-dns: {missing}'


def test_a_stack_after_dns_runs_on_its_own_gate_whatever_dns_returned() -> None:
    """A failed `dns`, the stack that reaches the LAN, stops neither stack that follows it.

    Each job's condition is evaluated over every combination of results the
    jobs up its chain can end in, and runs it exactly when its own gate
    passed: `up-k8s-base` the physical gate, `up-apps` `up-k8s-base`'s
    success. `up-dns`'s result is among the combinations and decides nothing.
    """
    findings: list[str] = []
    for name, gate in AFTER_DNS.items():
        _, chain, condition = _deploy_job(name)
        for combination in product(JOB_RESULTS, repeat=len(chain)):
            results = dict(zip(chain, combination, strict=True))
            if condition.runs(results) != gate(results):
                findings.append(f'{name} under {results}: runs={condition.runs(results)}, its gate={gate(results)}')

    assert findings == [], findings


#: A matrix property spelled inside a step's text, wherever it sits.
MATRIX_REFERENCE = re.compile(r'\$\{\{\s*matrix\.([\w-]+)\s*\}\}')
#: The `pulumi` options that take a value, so that the value is not read as
#: the subcommand.
PULUMI_VALUE_OPTIONS = frozenset({'--stack', '-s', '--cwd', '-C'})
#: The subcommands that refresh when given `--refresh`.
REFRESHING_UPDATES = frozenset({'preview', 'up', 'update'})


@dataclass(frozen=True)
class PulumiCommand:
    """One `pulumi` invocation a step runs: where, and the arguments after `pulumi`."""

    where: str
    arguments: tuple[str, ...]

    def _option(self, *names: str) -> str | None:
        for index, argument in enumerate(self.arguments):
            for name in names:
                if argument == name and index + 1 < len(self.arguments):
                    return self.arguments[index + 1]
                if argument.startswith(f'{name}='):
                    return argument.removeprefix(f'{name}=')
        return None

    @property
    def subcommand(self) -> str | None:
        """The first argument that is no option and no option's value."""
        skip = False
        for argument in self.arguments:
            if skip:
                skip = False
            elif argument in PULUMI_VALUE_OPTIONS:
                skip = True
            elif not argument.startswith('-'):
                return argument
        return None

    @property
    def stack(self) -> str | None:
        return self._option('--stack', '-s')

    @property
    def parallel(self) -> str | None:
        return self._option('--parallel', '-p')

    @property
    def refreshes(self) -> bool:
        """`pulumi refresh`, or an update or preview told to refresh first."""
        if self.subcommand == 'refresh':
            return True
        if self.subcommand not in REFRESHING_UPDATES:
            return False
        flags = [argument for argument in self.arguments if argument in {'--refresh', '-r'}]
        valued = [argument.split('=', 1)[1] for argument in self.arguments if argument.startswith('--refresh=')]
        return bool(flags) or any(value != 'false' for value in valued)

    @property
    def runs_the_program(self) -> bool:
        return '--run-program' in self.arguments or '--run-program=true' in self.arguments


#: The console scripts `pyproject.toml` declares.
PROJECT_SCRIPTS = frozenset(tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['scripts'])
#: Every console script a step runs, by the `pulumi` subcommand it runs over
#: the arguments it is given, or `None` for one that runs no `pulumi`
#: subcommand of the program: `stack-plan` previews the stack it is given
#: (`kluster.scripts.stack_plan.cli`), `state-backend` releases a lock or
#: prints pins, and `typecheck` runs `basedpyright`.
SCRIPT_SUBCOMMANDS: Mapping[str, str | None] = {'stack-plan': 'preview', 'state-backend': None, 'typecheck': None}


def _uv_run_script(simple: list[str], where: str) -> str | None:
    """The console script of this repository a simple command starts through `uv run`, if it starts one.

    An option between `uv run` and what it runs (`--frozen`, `--`) fails
    here rather than reading as no script, since the script behind it could
    run the program.
    """
    for index, token in enumerate(simple[:-2]):
        if token != 'uv' or simple[index + 1] != 'run':
            continue
        started = simple[index + 2]
        assert not started.startswith('-'), (
            f'{where}: `uv run` takes {started!r} ahead of what it runs, which this read does not follow'
        )
        if started in PROJECT_SCRIPTS:
            return started
    return None


def _expanded(text: str, row: dict[str, object] | None) -> str:
    """A step's text as one matrix combination makes it, or as written when the matrix cannot be read.

    A property the combination does not carry expands to the empty string, as
    GitHub dereferences a property an object lacks.
    """
    if row is None:
        return text
    return MATRIX_REFERENCE.sub(lambda reference: str(row.get(reference.group(1), '')), text)


def _pulumi_commands(text: str, where: str) -> list[PulumiCommand]:
    """Every `pulumi` invocation in a step's shell text, one simple command at a time.

    The text is split as the shell splits words, a quoted string across lines
    included, and cut at every newline and control operator, so `if mise x --
    pulumi preview …; then` reads as the preview it runs. A backslash that
    continues a line is read as the space it stands for. A step whose text
    never names `pulumi` or a `uv run` runs none and is not split at all.

    A console script of this repository that `uv run` starts is read as the
    `pulumi` subcommand `SCRIPT_SUBCOMMANDS` gives it, over the script's own
    arguments, and one that table does not name fails here, since it could
    run the program.
    """
    if 'pulumi' not in text and 'uv run' not in text:
        return []
    lexer = shlex.shlex(text.replace('\\\n', ' '), posix=True, punctuation_chars='();<>|&\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as error:
        raise AssertionError(f'{where}: a step does not split as shell words ({error})') from error
    commands: list[PulumiCommand] = []
    simple: list[str] = []
    for token in [*tokens, ';']:
        if token and set(token) <= set('();<>|&\n'):
            if 'pulumi' in simple:
                start = simple.index('pulumi') + 1
                commands.append(PulumiCommand(where, tuple(simple[start:])))
            elif (script := _uv_run_script(simple, where)) is not None:
                assert script in SCRIPT_SUBCOMMANDS, (
                    f'{where}: runs the console script {script!r}, which SCRIPT_SUBCOMMANDS does not read'
                )
                if (subcommand := SCRIPT_SUBCOMMANDS[script]) is not None:
                    start = simple.index(script) + 1
                    commands.append(PulumiCommand(where, (subcommand, *simple[start:])))
            simple = []
        else:
            simple.append(token)
    return commands


def _every_pulumi_command() -> list[PulumiCommand]:
    """Every `pulumi` invocation any workflow or composite action runs, one per matrix combination."""
    found: list[PulumiCommand] = []
    for path in workflows_and_actions():
        document = read_workflow(path)
        groups: dict[str, tuple[object, list[dict[str, object]] | None]]
        if path.parent.name == 'workflows':
            jobs = workflow_jobs(document, github_name(path))
            groups = {
                f'{github_name(path)}: {name}': (job.get('steps'), _matrix_rows(job, f'{github_name(path)}: {name}'))
                for name, job in jobs.items()
            }
        else:
            runs = mapping(document.get('runs'), f'{github_name(path)} runs:')
            groups = {github_name(path): (runs.get('steps'), [{}])}
        for where, (steps, rows) in groups.items():
            for step in cast('list[object]', steps or []):
                run = mapping(step, f'{where} step').get('run')
                if not isinstance(run, str):
                    continue
                for row in [None] if rows is None else rows:
                    found.extend(_pulumi_commands(_expanded(run, row), where))
    return found


def test_every_refresh_of_physical_runs_the_program_first() -> None:
    """A refresh builds each provider from state, before the program runs.

    `physical`'s libvirt provider reads its identity and its `known_hosts` by
    path, and only the program writes them (physical/homelab-host.md §6), so a
    refresh of that stack on a runner, whose checkout is fresh, dials with
    neither unless the program runs first. Every `pulumi` command a workflow or
    a composite action runs is read, in every matrix combination of its job,
    and each one that refreshes `physical` -- `pulumi refresh`, or a preview or
    update with `--refresh` -- must carry `--run-program`. A refresh whose
    stack this read cannot resolve fails here too, since it could be
    `physical`'s.
    """
    commands = _every_pulumi_command()
    refreshes = [command for command in commands if command.refreshes]
    unread = [command for command in refreshes if command.stack is None or '$' in command.stack]
    physical = [command for command in refreshes if command.stack == conventions.STACK_NAMES.physical]

    # A read that stopped finding the drift check's refresh would pass on nothing.
    assert physical, 'no workflow step refreshes physical'
    assert unread == [], f'refreshes whose stack is not read here: {unread}'
    assert [command for command in physical if not command.runs_the_program] == []


#: A stand-in for the whole of what the plan step runs, `mise x -- uv run
#: stack-plan …`: it exits with the code the case hands it.
VERDICT_STAND_IN = """#!/bin/sh
exit "$STAND_IN_EXIT"
"""
#: The step's bound in a case whose stand-in exits at once, so a step still
#: running at this bound is a hang.
PLAN_STEP_TIMEOUT = 20


def _plan_step() -> dict[str, object]:
    """`deploy.yml`'s `plan-physical` step whose outputs the job reads."""
    jobs = workflow_jobs(read_workflow(GITHUB / 'workflows' / 'deploy.yml'), 'deploy.yml')
    job = jobs['plan-physical']
    outputs = mapping(job.get('outputs'), 'deploy.yml plan-physical outputs:')
    reference = re.fullmatch(r'\$\{\{\s*steps\.([\w-]+)\.outputs\.diff\s*\}\}', str(outputs.get('diff')))
    assert reference, f'plan-physical outputs.diff is not a step output: {outputs.get("diff")!r}'
    steps = [mapping(step, 'a plan-physical step') for step in cast('list[object]', job.get('steps'))]
    (step,) = [step for step in steps if step.get('id') == reference.group(1)]
    return step


def _run_plan_step(
    tmp_path: Path, stand_ins: Mapping[str, str], env: Mapping[str, str], *, cwd: Path, timeout: float
) -> tuple[int, str, dict[str, str]]:
    """Run the plan step with `stand_ins` first on `PATH`, under `env`, in `cwd`.

    `stand_ins` maps a command's name to the script that stands in for it.
    Returns the step's exit, what it printed, and the outputs it wrote.
    """
    step = _plan_step()
    assert 'shell' not in step, 'the plan step names a shell, which this case does not model'
    run = step.get('run')
    assert isinstance(run, str)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, text in stand_ins.items():
        _ = (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    outputs = tmp_path / 'outputs'
    outputs.touch()
    path = env.get('PATH', os.environ['PATH'])
    full = {**env, 'PATH': f'{bin_dir}:{path}', 'GITHUB_OUTPUT': str(outputs)}
    # A step that names no shell runs as `bash -e {0}` on a Linux runner.
    done = process_sessions.run(['bash', '-e', '-c', run], cwd=cwd, env=full, text=True, timeout=timeout)
    written = dict(line.split('=', 1) for line in outputs.read_text().splitlines())
    return done.returncode, done.stdout + done.stderr, written


def _run_plan_step_on_a_verdict(tmp_path: Path, verdict: int) -> tuple[int, str, dict[str, str]]:
    """Run the plan step with what it runs answering `verdict`."""
    env = {'HOME': str(tmp_path), 'STAND_IN_EXIT': str(verdict)}
    return _run_plan_step(tmp_path, {'mise': VERDICT_STAND_IN}, env, cwd=tmp_path, timeout=PLAN_STEP_TIMEOUT)


def test_a_plan_that_would_write_nothing_skips_the_gated_apply(tmp_path: Path) -> None:
    code, output, written = _run_plan_step_on_a_verdict(tmp_path, stack_plan_cli.NOTHING)

    assert code == 0, output
    assert written == {'diff': 'false'}


def test_a_plan_that_would_write_routes_to_the_gated_apply(tmp_path: Path) -> None:
    code, output, written = _run_plan_step_on_a_verdict(tmp_path, stack_plan_cli.WOULD_WRITE)

    assert code == 0, output
    assert written == {'diff': 'true'}


@pytest.mark.parametrize('verdict', [stack_plan_cli.NO_ANSWER, 2, 255])
def test_a_plan_with_no_answer_fails_its_job_by_name_and_proposes_nothing(tmp_path: Path, verdict: int) -> None:
    """A preview that failed -- a plugin it could not install, a provider it could not reach -- is no plan.

    Read as a diff, it would leave `up-physical` waiting for an approval with
    nothing to approve, holding the deploy group (kluster-ops#778).
    """
    code, output, written = _run_plan_step_on_a_verdict(tmp_path, verdict)

    assert code != 0, output
    assert written == {}
    assert f'::error::the physical plan has no answer (exit {verdict})' in output


#: A program of one stand-in resource, a control plane, which it declares
#: protected while `PROBE_PROTECT` is `true`.
PROTECTED_PROGRAM = """\
import os

import pulumi
from pulumi.dynamic import CreateResult, Resource, ResourceProvider


class Provider(ResourceProvider):
    def create(self, props):
        return CreateResult(id_='stand-in', outs=dict(props))


class Stand(Resource):
    def __init__(self, name, props, opts=None):
        super().__init__(Provider(), name, props, opts)


Stand(
    'control-plane',
    {'value': 'as it was'},
    pulumi.ResourceOptions(protect=os.environ.get('PROBE_PROTECT') == 'true'),
)
"""

#: `uv run <script>` runs the console script of that name the test run has
#: installed, for every script `pyproject.toml` declares; anything else is the
#: real `uv`'s, which the program's language host also calls.
UV_RUNS_THE_PROJECTS_SCRIPT = """#!/bin/sh
if [ "$1" = run ]; then
  for script in {scripts}; do
    if [ "$2" = "$script" ]; then
      shift 2
      exec {installed}/"$script" "$@"
    fi
  done
fi
exec {uv} "$@"
"""

#: The bound of a case that runs the pinned CLI: its project's set-up, the
#: stack's creation, a baseline `up` and the plan step, whose command runs two
#: previews. On 24 cores at a load average under 10, each case took 4 s. A
#: stop-loss, which nothing asserts on, set as `tests/test_stack_plan_engine.py`
#: sets its own, which runs the same command against a larger program.
ENGINE_CASE_TIMEOUT = 240
#: The bound of one command of such a case, the plan step's included: a
#: stop-loss below the case's, so a stalled command fails naming itself.
ENGINE_COMMAND_TIMEOUT = 120


@pytest.mark.skipif(
    shutil.which('pulumi') is None or shutil.which('uv') is None, reason='the pinned pulumi CLI or uv is not on PATH'
)
@pytest.mark.timeout(ENGINE_CASE_TIMEOUT)
@pytest.mark.parametrize(
    ('protect', 'diff'), [('false', 'false'), ('true', 'true')], ids=['unchanged', 'protect set and no step']
)
def test_the_plan_step_routes_to_the_gated_apply_whatever_an_up_would_write(
    tmp_path: Path, protect: str, diff: str
) -> None:
    """The plan step, run as written against the pinned CLI, reads `physical`'s plan as the `up` would write it.

    Aetf/kluster#753's deploy: a merge that declares a resource of `physical`
    protected, and changes nothing else, plans no step, and an `up` writes
    the `protect` all the same. The step must route it to the gated apply,
    or the `protect` is never written and the engine, which reads it from the
    state, refuses no delete. An unchanged program, after the same baseline,
    still skips it. The step's own text runs, with `mise` running its
    command, `uv run` the console script this test run installed, and
    `pulumi` the pinned CLI over a scratch backend (framework/testing.md §8),
    under the switches `mise.toml` gives every CI run.
    """
    root = tmp_path / 'scratch'
    root.mkdir()
    scratch = scratch_project(root, PROTECTED_PROGRAM)
    env = scratch.env | stack_environment.state_switches()
    stack = conventions.STACK_NAMES.physical
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1): the `up` below is of a stack named
    # `physical`.
    listed = process_sessions.run(
        ['pulumi', '--non-interactive', 'stack', 'ls', '--all', '--json'],
        cwd=scratch.directory,
        env=env,
        text=True,
        timeout=ENGINE_COMMAND_TIMEOUT,
    )
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    for command in (['stack', 'init', stack], ['up', '--yes', '--skip-preview', '--stack', stack]):
        done = process_sessions.run(
            ['pulumi', '--non-interactive', *command],
            cwd=scratch.directory,
            env=env,
            text=True,
            timeout=ENGINE_COMMAND_TIMEOUT,
        )
        assert done.returncode == 0, done.stdout + done.stderr
    uv = shutil.which('uv')
    assert uv is not None
    stand_ins = {
        'mise': MISE_STAND_IN,
        'uv': UV_RUNS_THE_PROJECTS_SCRIPT.format(
            scripts=' '.join(shlex.quote(script) for script in sorted(PROJECT_SCRIPTS)),
            installed=shlex.quote(str(Path(sys.executable).parent)),
            uv=shlex.quote(uv),
        ),
    }

    code, output, written = _run_plan_step(
        tmp_path,
        stand_ins,
        env | {'PROBE_PROTECT': protect},
        cwd=scratch.directory,
        timeout=ENGINE_COMMAND_TIMEOUT,
    )

    assert code == 0, output
    assert written == {'diff': diff}, output


#: Stand-ins for what a deploy's `Up` step runs. `mise x -- <command>` runs the
#: command. `pulumi` logs its own pid, the time it started in seconds and what
#: it was asked to run, then exits with `UP_EXIT`, or kills itself with the
#: signal `UP_SIGNAL` names. `uv` logs what it was asked to run and exits with
#: `RELEASE_EXIT`.
MISE_STAND_IN = """#!/bin/sh
[ "$1" = x ] && [ "$2" = -- ] || exit 90
shift 2
exec "$@"
"""
PULUMI_STAND_IN = """#!/bin/sh
printf 'pulumi %s %s %s\\n' "$$" "$(date +%s)" "$*" >> "$STAND_IN_LOG"
[ -z "$UP_SIGNAL" ] || kill -s "$UP_SIGNAL" $$
exit "$UP_EXIT"
"""
UV_STAND_IN = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$STAND_IN_LOG"
exit "$RELEASE_EXIT"
"""
#: The step's bound in a case: every stand-in exits at once.
UP_STEP_TIMEOUT = 20


def _up_steps() -> dict[str, dict[str, object]]:
    """Every step of `deploy.yml` that runs `pulumi up`, by its job's name."""
    where = github_name(DEPLOY)
    found: dict[str, dict[str, object]] = {}
    for name, job in workflow_jobs(read_workflow(DEPLOY), where).items():
        for entry in cast('list[object]', job.get('steps') or []):
            step = mapping(entry, f'{where}: {name} step')
            if any(command.subcommand == 'up' for command in _step_pulumi_commands(step, f'{where}: {name}')):
                found[name] = step
    # A read that stopped finding the applies would pass on nothing.
    assert 'up-physical' in found, found
    return found


@dataclass(frozen=True)
class StandInRun:
    """One run of the `pulumi` stand-in: the pid it ran as, the second it started in, and what it was asked."""

    pid: int
    started: int
    command: PulumiCommand


@dataclass(frozen=True)
class UpStepRun:
    """What an `Up` step did: its exit and output, each `pulumi` it ran, and each `release-lock` it ran."""

    code: int
    output: str
    pulumi: list[StandInRun]
    releases: list[list[str]]


def _run_up_step(
    tmp_path: Path,
    step: dict[str, object],
    *,
    up_exit: int,
    signal: str = '',
    release_exit: int = 0,
    workflow_env: Mapping[str, str] | None = None,
) -> UpStepRun:
    """The step run against the stand-ins, with `workflow_env` in its environment as the runner gives a step its `env`."""
    assert 'shell' not in step, 'the Up step names a shell, which this case does not model'
    run = step.get('run')
    assert isinstance(run, str)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(parents=True)
    for name, text in {'mise': MISE_STAND_IN, 'pulumi': PULUMI_STAND_IN, 'uv': UV_STAND_IN}.items():
        _ = (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    runner_temp = tmp_path / 'runner-temp'
    runner_temp.mkdir()
    log = tmp_path / 'log'
    log.touch()
    env = {
        **(workflow_env or {}),
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'HOME': str(tmp_path),
        'RUNNER_TEMP': str(runner_temp),
        'STAND_IN_LOG': str(log),
        'UP_EXIT': str(up_exit),
        'UP_SIGNAL': signal,
        'RELEASE_EXIT': str(release_exit),
    }
    # A step that names no shell runs as `bash -e {0}` on a Linux runner. It
    # starts with SIGHUP at its default, whatever this process inherited: a
    # suite launched under `nohup` hands its children SIGHUP ignored, a shell
    # can neither trap nor reset a signal ignored on entry, and the HUP the
    # `pulumi` stand-in sends itself would then end nothing.
    argv = ['env', '--default-signal=HUP', 'bash', '-e', '-c', run]
    done = process_sessions.run(argv, cwd=tmp_path, env=env, text=True, timeout=UP_STEP_TIMEOUT)
    pulumi: list[StandInRun] = []
    releases: list[list[str]] = []
    for line in log.read_text().splitlines():
        tool, rest = line.split(' ', 1)
        if tool == 'pulumi':
            pid, started, asked = rest.split(' ', 2)
            command = PulumiCommand('the stand-in log', tuple(shlex.split(asked)))
            pulumi.append(StandInRun(pid=int(pid), started=int(started), command=command))
        else:
            releases.append(shlex.split(rest))
    return UpStepRun(code=done.returncode, output=done.stdout + done.stderr, pulumi=pulumi, releases=releases)


def _released(release: list[str]) -> tuple[str, int, int]:
    """The stack a `uv run state-backend release-lock …` names, and the pid and time in the file it names."""
    assert release[:3] == ['run', 'state-backend', 'release-lock'], release
    options = dict(zip(release[3::2], release[4::2], strict=True))
    pid, started = (int(field) for field in Path(options['--start-file']).read_text().split())
    return options['--stack'], pid, started


#: Statuses `pulumi` exits with by itself, from the exit-code table of the
#: pinned CLI (`pkg/cmd/pulumi/cmd/exit_codes.go` at 3.274.1): success, a
#: generic error, `ExitCancelled` and `ExitInternalError`.
PULUMI_EXITS = (0, 1, 8, 255)


@pytest.mark.parametrize('up_exit', PULUMI_EXITS)
def test_an_up_that_exits_by_itself_has_the_lock_its_pulumi_left_released(tmp_path: Path, up_exit: int) -> None:
    """A `pulumi` reports a failed delete of its own lock as a diagnostic, and exits as its update made it.

    So the step hands `release-lock` the stack it applied, the pid that
    `pulumi` ran as and a time no later than its start, after a success as
    after a failure, and keeps the status `pulumi` gave (framework/ci.md
    §3.3).
    """
    for job, step in _up_steps().items():
        ran = _run_up_step(tmp_path / job, step, up_exit=up_exit)

        assert ran.code == up_exit, f'{job}: {ran.output}'
        (pulumi,) = ran.pulumi
        assert pulumi.command.subcommand == 'up'
        ((stack, pid, started),) = [_released(release) for release in ran.releases]
        assert (stack, pid) == (pulumi.command.stack, pulumi.pid), job
        assert started <= pulumi.started, job


def test_mise_x_runs_its_command_as_its_own_process() -> None:
    """The pid an `Up` step records is `pulumi`'s only because `mise x` replaces itself with what it runs.

    The stand-in above does the same, so the cases above hold the step to
    that pid and this one holds the `mise` on `PATH` -- the pinned release, on
    a runner -- to the stand-in. A `mise` that ran its command as a child
    instead would hand `release-lock` a pid no lock names, and every lock a
    deploy left would wait for the hand procedure.
    """
    mise = shutil.which('mise')
    if mise is None:
        pytest.skip('mise is not on PATH')
    with process_sessions.started(
        [mise, 'x', '--', 'sh', '-c', 'echo "$$"'], cwd=ROOT, stdout=process_sessions.PIPE, text=True
    ) as command:
        printed, _ = command.communicate(timeout=UP_STEP_TIMEOUT)

    assert command.returncode == 0
    assert int(printed.split()[-1]) == command.pid


#: Signals the pinned `pulumi` does not trap during an update: it traps SIGINT
#: and SIGTERM there, cancels and exits by itself (`signal.Notify` in
#: `pkg/backend/cancellation_scope.go` at 3.274.1).
UNTRAPPED_SIGNALS = ('KILL', 'HUP')


@pytest.mark.parametrize('signal', UNTRAPPED_SIGNALS)
def test_an_up_a_signal_ended_leaves_its_lock_to_the_hand_procedure(tmp_path: Path, signal: str) -> None:
    """A killed apply never tried its own delete, and may have hung where the next one would (§3.3)."""
    for job, step in _up_steps().items():
        ran = _run_up_step(tmp_path / job, step, up_exit=0, signal=signal)

        assert ran.code > 128, f'{job}: {ran.output}'
        assert ran.releases == [], job


@pytest.mark.parametrize('up_exit', [0, 4])
def test_a_lock_left_uncleared_fails_the_step_by_name(tmp_path: Path, up_exit: int) -> None:
    """The next `up` refuses on a lock left behind, so a release that could not finish fails the apply that took it."""
    for job, step in _up_steps().items():
        ran = _run_up_step(tmp_path / job, step, up_exit=up_exit, release_exit=1)

        assert ran.code == (up_exit or 1), f'{job}: {ran.output}'
        (pulumi,) = ran.pulumi
        assert f'::error::the lock this run took on {pulumi.command.stack} may be left' in ran.output, ran.output


#: The variable of `deploy.yml`'s own `env` that holds how many resource
#: operations an apply of a stack in `CLUSTER_OBJECT_STACKS` runs at once.
APPLY_PARALLEL = 'CLUSTER_APPLY_PARALLEL'
#: The stacks whose objects start pods on the control planes, so whose first
#: `up` writes beside etcd on their boot volumes (cluster/nodes.md §1.2).
CLUSTER_OBJECT_STACKS = frozenset({conventions.STACK_NAMES.k8s_base, conventions.STACK_NAMES.apps})


def _written_env(block: object, where: str) -> dict[str, str]:
    """The variables of an `env:` block whose values are written out, as the runner hands them to a step: as text."""
    if block is None:
        return {}
    return {name: str(value) for name, value in mapping(block, where).items() if '${{' not in str(value)}


def test_every_up_of_a_stack_that_starts_pods_runs_at_the_workflows_apply_parallelism(tmp_path: Path) -> None:
    """`--parallel` bounds how many workloads start together on a control plane's boot volume (nodes.md §1.2).

    The bound is held in one place, the workflow's own `env`, so each case
    runs every `Up` step with that variable set to a value the workflow does
    not hold, layered under the job's and the step's own `env` as the runner
    layers them: an apply of a stack in `CLUSTER_OBJECT_STACKS` passes that
    value, and so reads the bound from that place rather than from a copy of
    it, in its command or in a job's `env`.
    """
    where = github_name(DEPLOY)
    workflow = read_workflow(DEPLOY)
    held = _written_env(workflow.get('env'), f'{where} env:')
    assert APPLY_PARALLEL in held, f'{where} holds no {APPLY_PARALLEL} in its own env'
    stand_in = str(int(held[APPLY_PARALLEL]) + 1)
    jobs = workflow_jobs(workflow, where)
    applied: set[str] = set()
    for job, step in _up_steps().items():
        env = {
            **held,
            APPLY_PARALLEL: stand_in,
            **_written_env(jobs[job].get('env'), f'{where}: {job} env:'),
            **_written_env(step.get('env'), f'{where}: {job} Up env:'),
        }
        ran = _run_up_step(tmp_path / job, step, up_exit=0, workflow_env=env)

        assert ran.code == 0, f'{job}: {ran.output}'
        (pulumi,) = ran.pulumi
        if pulumi.command.stack in CLUSTER_OBJECT_STACKS:
            applied.add(pulumi.command.stack)
            assert pulumi.command.parallel == stand_in, f'{job}: {pulumi.command.arguments}'
    # A read that stopped finding these applies would pass on nothing.
    assert applied == CLUSTER_OBJECT_STACKS


#: The `pulumi` subcommands that run the program, and with it every plugin it
#: needs.
PROGRAM_SUBCOMMANDS = frozenset({'preview', 'up', 'update', 'refresh', 'destroy'})
#: The job's own token, as a step spells it.
JOB_TOKEN = re.compile(r'^\$\{\{\s*(?:github\.token|secrets\.GITHUB_TOKEN)\s*\}\}$')


def _step_pulumi_commands(step: dict[str, object], where: str) -> list[PulumiCommand]:
    run = step.get('run')
    return _pulumi_commands(run, where) if isinstance(run, str) else []


def _installs_the_projects_plugins(command: PulumiCommand) -> bool:
    """`pulumi plugin install` with no plugin named: the set the project's packages name."""
    arguments = [argument for argument in command.arguments if not argument.startswith('-')]
    return arguments == ['plugin', 'install']


def test_every_job_that_runs_the_program_installs_its_plugins_first_with_the_jobs_token() -> None:
    """A plugin fetched in the middle of a run fails it at random; the token is the install's alone.

    The talos provider's plugin is fetched through GitHub's API, which limits
    an unauthenticated address, and a runner's address is shared
    (framework/ci.md §3). So every job whose steps run the program installs
    the project's plugins in a step before the first of them, with
    `GITHUB_TOKEN` set to the job's own token, which the CLI sends. And no
    step that runs the program, nor the job around it, carries
    `GITHUB_TOKEN`, which the github provider takes as its credential.

    A composite action's steps are not ordered against the job's here, so an
    action that runs the program fails instead.
    """
    in_actions = [
        command
        for path in workflows_and_actions()
        if path.parent.name != 'workflows'
        for step in cast('list[object]', mapping(read_workflow(path).get('runs'), github_name(path)).get('steps') or [])
        for command in _step_pulumi_commands(mapping(step, github_name(path)), github_name(path))
        if command.subcommand in PROGRAM_SUBCOMMANDS
    ]
    assert in_actions == [], f'a composite action runs the program: {in_actions}'

    held: list[str] = []
    findings: list[str] = []
    for path in workflows_and_actions():
        if path.parent.name != 'workflows':
            continue
        for name, job in workflow_jobs(read_workflow(path), github_name(path)).items():
            where = f'{github_name(path)}: {name}'
            steps = [mapping(step, f'{where} step') for step in cast('list[object]', job.get('steps') or [])]
            program = [
                index
                for index, step in enumerate(steps)
                if any(command.subcommand in PROGRAM_SUBCOMMANDS for command in _step_pulumi_commands(step, where))
            ]
            if not program:
                continue
            held.append(where)
            job_env = mapping(job.get('env', {}), f'{where} env:')
            if 'GITHUB_TOKEN' in job_env:
                findings.append(f'{where}: the job sets GITHUB_TOKEN for every step')
            installs = [
                index
                for index, step in enumerate(steps[: program[0]])
                if any(_installs_the_projects_plugins(command) for command in _step_pulumi_commands(step, where))
            ]
            if not installs:
                findings.append(f'{where}: no step installs the project plugins before step {program[0]}')
            for index in installs:
                token = mapping(steps[index].get('env', {}), f'{where} step {index} env:').get('GITHUB_TOKEN')
                if not (isinstance(token, str) and JOB_TOKEN.match(token)):
                    findings.append(f"{where} step {index}: the plugin install's GITHUB_TOKEN is {token!r}")
            for index in program:
                if 'GITHUB_TOKEN' in mapping(steps[index].get('env', {}), f'{where} step {index} env:'):
                    findings.append(f'{where} step {index}: runs the program with GITHUB_TOKEN set')

    # A read that stopped finding the plan and the apply would pass on nothing.
    assert {'workflows/deploy.yml: plan-physical', 'workflows/deploy.yml: up-physical'} <= set(held), held
    assert findings == [], findings
