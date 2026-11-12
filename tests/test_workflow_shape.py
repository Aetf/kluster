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
`physical` join waits for the gateway's shell, a `dns` join for nothing.

And the merge chain applies in the order that section gives it: `up-k8s-base`
and `up-apps` follow `up-dns`, each on a gate of its own, whatever `up-dns`
returned. The conditions are read by a model of how GitHub evaluates a job's
`if:` and evaluated over every result the jobs up the chain can end in, so a
condition written another way is held to what it does rather than to how it
is spelled.

The workflow files are read the way the other seams read them, through the
loaders in `workflow_files`.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from renovate_text import as_python_spells_it
from workflow_files import GITHUB, github_name, mapping, read_workflow, workflow_jobs, workflows_and_actions

from kluster import conventions
from kluster.components.overlay.flow_rules import SSH_PORT
from kluster.scripts.credentials import slots

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


@dataclass(frozen=True)
class Join:
    """One way a job can join the overlay: where, in which Environment, and what it waits for."""

    where: str
    environment: str
    wait_for: str


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
                Join(where, _resolved(environment, row, where), _resolved(inputs.get('wait_for'), row, where))
                for row in rows
            )
    return joins


def test_a_physical_join_waits_for_the_gateways_shell_and_a_dns_join_waits_for_nothing() -> None:
    """Which identity a job joins with decides whether its join waits, and on what.

    The identity is the Environment's secret, so the Environment decides it
    (`slots.ZEROTIER_PHYSICAL`, `slots.ZEROTIER_DNS`). A `physical` run's first
    dial is the device-files push's single connect to the gateway's shell, and
    the flow rules can drop a fresh member's first packets (physical/gateway.md
    §2.3), so its join waits for that shell; the readiness line it prints is
    where §2.4 there reads items 4 and 7. A `dns` run reaches the resolvers,
    each a leaf whose absence fails only its own rewrites (§3 there), so a wait
    on one would fail the whole job, and its join waits for nothing.
    """
    joins = _joins()
    physical = [join for join in joins if join.environment in slots.ZEROTIER_PHYSICAL]
    dns = [join for join in joins if join.environment in slots.ZEROTIER_DNS]

    # A read that stopped finding either kind would pass on nothing.
    assert physical, 'no job joins with the physical identity'
    assert dns, 'no job joins with the dns identity'
    # An Environment holding neither identity has nothing to join with.
    assert [join for join in joins if join not in physical + dns] == []
    gateway_shell = f'{conventions.overlay.UDM}:{SSH_PORT}'
    assert [join for join in physical if join.wait_for != gateway_shell] == []
    assert [join for join in dns if join.wait_for] == []


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
