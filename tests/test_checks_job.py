"""`checks` runs its fast path under the `stack-files` scope, and every step otherwise.

Under `stack-files` the job runs its setup, the `Scope` step, the tests marked
`stack_files` and the prose step, and skips every other step; under `full`,
and on a push whatever the scope would say, it runs every step and the whole
suite (framework/ci.md §3). The verdict itself is `checks-scope`'s, which
`tests/test_checks_scope.py` holds; this holds the job to it.

The job is held by running it, in the form `tests/test_prose_step.py` runs
the prose step. Its steps are taken in order from `checks.yml`, each `if:`
and each `${{ … }}` in a step's `env` evaluated against the event and the
outputs earlier steps wrote, and each `run` executed as it stands under the
shell GitHub gives it, in a scratch repository, with stand-ins for `mise` and
`sudo` first on `PATH`. A `uses:` step is taken as run and executes nothing.
The `mise` stand-in logs every invocation with the step it ran in, exits 0
and prints nothing, except that a `checks-scope` invocation writes the scope
the case scripts into the file `--github-output` names, as the command does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import process_sessions
import pytest
from committed_stack_files import FIXTURE, MARKER, StackFiles
from workflow_files import GITHUB, mapping, read_workflow, workflow_jobs

from kluster.scripts.checks_scope import FULL, STACK_FILES

ROOT = Path(__file__).parent.parent

#: A stand-in for `mise`: every invocation is logged as JSON, with the step it
#: ran in; a `checks-scope` one writes `scope=<STAND_IN_SCOPE>` into its
#: `--github-output` file, and writes nothing for an empty scope.
STAND_IN_MISE = f"""#!{sys.executable}
import json, os, sys
arguments = sys.argv[1:]
with open(os.environ['STAND_IN_LOG'], 'a') as log:
    log.write(json.dumps({{'step': os.environ['STAND_IN_STEP'], 'argv': arguments}}) + '\\n')
if 'checks-scope' in arguments:
    scope = os.environ['STAND_IN_SCOPE']
    if scope:
        output = arguments[arguments.index('--github-output') + 1]
        with open(output, 'a') as file:
            file.write(f'scope={{scope}}\\n')
"""
#: A stand-in for `sudo`, which the podman step calls: it runs nothing.
STAND_IN_SUDO = '#!/bin/sh\nexit 0\n'

#: Each git call's bound, and each step's: a stop-loss, which nothing asserts
#: on. Summed over a case's steps they stay below its 60 s bound.
GIT_TIMEOUT = 5
STEP_TIMEOUT = 5

#: The steps the fast path runs: the setup, the verdict, the tests and the
#: prose, which a change of this shape can reach (framework/ci.md §3).
FAST_PATH = {'Install dependencies', 'Scope', 'Tests', 'Prose'}


def checks_steps() -> list[dict[str, object]]:
    path = GITHUB / 'workflows' / 'checks.yml'
    steps = workflow_jobs(read_workflow(path), 'checks.yml')['checks']['steps']
    assert isinstance(steps, list)
    return [mapping(step, 'a checks.yml step') for step in cast('list[object]', steps)]


def checks_env() -> dict[str, str]:
    path = GITHUB / 'workflows' / 'checks.yml'
    env = workflow_jobs(read_workflow(path), 'checks.yml')['checks'].get('env', {})
    return {str(name): str(value) for name, value in mapping(env, 'the checks job env').items()}


# -- the expressions the job is written in ---------------------------------------

#: A token of an expression, by kind: a string literal, an operator or
#: punctuation, a name. Anything else stops the read, so an expression in a
#: form this does not model fails rather than reading as something it is not.
TOKEN = re.compile(r"\s*(?:(?P<string>'[^']*')|(?P<operator>==|!=|&&|\|\||[().])|(?P<name>[A-Za-z_][\w-]*))")
#: An expression inside a string: `${{ … }}`.
INTERPOLATION = re.compile(r'\$\{\{(.*?)\}\}', re.S)

#: What an expression evaluates to: a string, a boolean, or `None` for a
#: property the context does not hold, which GitHub's `null` is.
type Value = str | bool | None


def evaluate(expression: str, context: Mapping[str, object]) -> Value:
    """`expression` as GitHub evaluates it, over the forms the job is written in.

    String literals, property paths into `context`, `==` and `!=` (which
    ignore case, as GitHub's do), and `&&` and `||`, which yield an operand as
    GitHub's do. `!=` and `==` bind tighter than `&&`, which binds tighter than
    `||`. A property `context` does not hold is `None`. Anything else is
    refused while the expression is read.
    """
    tokens: list[tuple[str, str]] = []
    position = 0
    while expression[position:].strip():
        found = TOKEN.match(expression, position)
        assert found and found.lastgroup, f'{expression[position:]!r} is not read here'
        tokens.append((found.lastgroup, found.group(found.lastgroup)))
        position = found.end()
    reader = _Reader(tokens, context)
    value = reader.disjunction()
    assert reader.position == len(tokens), f'{expression!r} has text past its end'
    return value


def _truthy(value: Value) -> bool:
    return bool(value)


def _equal(left: Value, right: Value) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return left == right


@dataclass
class _Reader:
    tokens: list[tuple[str, str]]
    context: Mapping[str, object]
    position: int = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        assert token is not None, 'the expression ends early'
        self.position += 1
        return token

    def _chain(self, operator: str, operand: Callable[[], Value], combine: Callable[[Value, Value], Value]) -> Value:
        value = operand()
        while self._peek() == ('operator', operator):
            _ = self._take()
            value = combine(value, operand())
        return value

    def disjunction(self) -> Value:
        return self._chain('||', self.conjunction, lambda left, right: left if _truthy(left) else right)

    def conjunction(self) -> Value:
        return self._chain('&&', self.comparison, lambda left, right: right if _truthy(left) else left)

    def comparison(self) -> Value:
        value = self.primary()
        token = self._peek()
        if token in (('operator', '=='), ('operator', '!=')):
            _ = self._take()
            equal = _equal(value, self.primary())
            return equal if token == ('operator', '==') else not equal
        return value

    def primary(self) -> Value:
        kind, text = self._take()
        if (kind, text) == ('operator', '('):
            value = self.disjunction()
            assert self._take() == ('operator', ')'), 'an unclosed parenthesis'
            return value
        if kind == 'string':
            return text[1:-1]
        assert kind == 'name', f'{text!r} cannot start an operand'
        node: object = self.context.get(text)
        while self._peek() == ('operator', '.'):
            _ = self._take()
            kind, name = self._take()
            assert kind == 'name', f'{name!r} is not a property name'
            node = cast('Mapping[str, object]', node).get(name) if isinstance(node, Mapping) else None
        assert node is None or isinstance(node, str | bool), f'{text} is not a value'
        return node


def interpolated(text: str, context: Mapping[str, object]) -> str:
    """`text` with each `${{ … }}` replaced by its value, as a string; `None` is the empty string."""

    def value(found: re.Match[str]) -> str:
        result = evaluate(found.group(1), context)
        return '' if result is None else str(result).lower() if isinstance(result, bool) else result

    return INTERPOLATION.sub(value, text)


def runs(step: Mapping[str, object], context: Mapping[str, object]) -> bool:
    """Whether a step runs, every step before it having succeeded: its `if:`, or no condition at all."""
    condition = step.get('if')
    if condition is None:
        return True
    text = str(condition).strip()
    whole = INTERPOLATION.fullmatch(text)
    return _truthy(evaluate(whole.group(1) if whole else text, context))


def test_the_reading_of_an_expression_is_githubs() -> None:
    """`==` ignores case, a missing property is `null`, and `&&` and `||` yield an operand, as GitHub's do."""
    context = {'github': {'event_name': 'pull_request'}, 'steps': {'scope': {'outputs': {'scope': 'full'}}}}

    assert evaluate("github.event_name == 'PULL_REQUEST'", context) is True
    assert evaluate("steps.scope.outputs.scope != 'stack-files'", context) is True
    assert evaluate("steps.absent.outputs.scope != 'stack-files'", context) is True
    assert evaluate("github.event_name == 'pull_request' && 'HEAD^1' || github.event.before", context) == 'HEAD^1'
    assert evaluate("github.event_name == 'push' && 'HEAD^1' || 'before'", context) == 'before'
    assert interpolated('${{ steps.absent.outputs.scope }}', context) == ''


# -- the job, run ----------------------------------------------------------------


@dataclass
class Ran:
    """What one run of the job did."""

    #: The steps that ran, by name, in order.
    steps: list[str] = field(default_factory=list[str])
    #: Every `mise` invocation, with the step it ran in.
    calls: list[tuple[str, list[str]]] = field(default_factory=list[tuple[str, list[str]]])

    def commands(self, step: str) -> list[list[str]]:
        return [argv for name, argv in self.calls if name == step]


def _environment(home: Path) -> dict[str, str]:
    """The caller's `PATH` and none of their git, for the reason `tests/test_prose_step.py` gives."""
    return {
        'PATH': os.environ['PATH'],
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
    }


def run_job(tmp_path: Path, *, event: str, scope: str) -> Ran:
    """Run the `checks` job for `event`, its `checks-scope` answering `scope`."""
    repo = tmp_path / 'repo'
    (repo / '.vscode').mkdir(parents=True)
    for name in ('dictionary', 'disabledRules'):
        shutil.copy(ROOT / '.vscode' / f'ltex.{name}.en-US.txt', repo / '.vscode')
    for args in (['init', '-q'], ['add', '.']):
        _ = process_sessions.run(['git', *args], cwd=repo, env=_environment(tmp_path), check=True, timeout=GIT_TIMEOUT)

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, text in (('mise', STAND_IN_MISE), ('sudo', STAND_IN_SUDO)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / 'log'
    log.touch()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()

    outputs: dict[str, dict[str, dict[str, str]]] = {}
    context: dict[str, object] = {'github': {'event_name': event, 'event': {'before': ''}}, 'steps': outputs}
    ran = Ran()
    for step in checks_steps():
        name = str(step.get('name', step.get('uses')))
        if not runs(step, context):
            continue
        ran.steps.append(name)
        run = step.get('run')
        if run is None:
            continue
        output = tmp_path / f'output-{len(ran.steps)}'
        output.touch()
        env = {
            **_environment(tmp_path),
            **checks_env(),
            **{
                str(key): interpolated(str(value), context)
                for key, value in mapping(step.get('env', {}), f'the env of {name}').items()
            },
            'PATH': f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
            'GITHUB_OUTPUT': str(output),
            'RUNNER_TEMP': str(tmp_path),
            'XDG_RUNTIME_DIR': str(runtime),
            'STAND_IN_LOG': str(log),
            'STAND_IN_STEP': name,
            'STAND_IN_SCOPE': scope,
        }
        # GitHub's `bash` shell: `bash --noprofile --norc -eo pipefail {0}`.
        done = process_sessions.run(
            ['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', str(run)],
            cwd=repo,
            env=env,
            text=True,
            timeout=STEP_TIMEOUT,
        )
        assert done.returncode == 0, f'{name}: {done.stdout}{done.stderr}'
        if 'id' in step:
            written = dict(line.split('=', 1) for line in output.read_text().splitlines() if '=' in line)
            outputs[str(step['id'])] = {'outputs': written}
    for line in log.read_text().splitlines():
        call = json.loads(line)
        ran.calls.append((call['step'], call['argv']))
    return ran


def pytest_runs(ran: Ran) -> list[list[str]]:
    """Each `pytest` invocation, by its arguments after `pytest`."""
    return [argv[argv.index('pytest') + 1 :] for argv in ran.commands('Tests') if 'pytest' in argv]


def every_step() -> set[str]:
    return {str(step.get('name', step.get('uses'))) for step in checks_steps()}


def uses_steps() -> set[str]:
    return {str(step.get('name', step.get('uses'))) for step in checks_steps() if 'run' not in step}


def test_under_stack_files_only_the_fast_path_runs_and_the_tests_are_the_stack_file_readers(tmp_path: Path) -> None:
    ran = run_job(tmp_path, event='pull_request', scope=STACK_FILES)

    assert set(ran.steps) == FAST_PATH | uses_steps()
    assert pytest_runs(ran) == [['-q', '-m', 'stack_files']]
    assert {step for step, _ in ran.calls} <= FAST_PATH


@pytest.mark.parametrize('scope', [FULL, ''], ids=['full', 'no-verdict'])
def test_a_pull_request_out_of_the_shape_runs_every_step_and_the_whole_suite(tmp_path: Path, scope: str) -> None:
    ran = run_job(tmp_path, event='pull_request', scope=scope)

    assert set(ran.steps) == every_step()
    assert pytest_runs(ran) == [['-q']]


def test_a_push_runs_every_step_and_the_whole_suite_whatever_the_scope(tmp_path: Path) -> None:
    ran = run_job(tmp_path, event='push', scope=STACK_FILES)

    assert set(ran.steps) == every_step() - {'Scope'}
    assert ran.commands('Scope') == []
    assert pytest_runs(ran) == [['-q']]


def test_the_scope_compares_against_the_base_the_prose_step_diffs_against(tmp_path: Path) -> None:
    ran = run_job(tmp_path, event='pull_request', scope=FULL)
    (scope,) = ran.commands('Scope')
    (prose,) = [step for step in checks_steps() if step.get('name') == 'Prose']
    context = {'github': {'event_name': 'pull_request', 'event': {'before': ''}}}
    base = interpolated(str(mapping(prose['env'], 'the env of Prose')['BASE']), context)

    assert base
    assert scope[scope.index('--base') + 1] == base


# -- the selection the fast path runs --------------------------------------------

#: A suite of three cases: one asking for the fixture, one reaching it through
#: a fixture of its own, and one reading nothing.
SELECTION_SUITE = f"""
import pytest

@pytest.fixture
def stacks({FIXTURE}):
    return {FIXTURE}

def test_asks_for_it({FIXTURE}):
    assert {FIXTURE}.stacks()

def test_reaches_it(stacks):
    assert stacks.stacks()

def test_reads_nothing():
    pass
"""

#: The bound on the child `pytest`: a stop-loss, which nothing asserts on.
SELECTION_TIMEOUT = 30


def selected(tmp_path: Path, *arguments: str) -> tuple[set[str], str]:
    """The cases a child `pytest` with the plugin loaded runs over `SELECTION_SUITE`, and what it printed.

    The child loads `committed_stack_files` alone, as `tests/conftest.py`
    loads it, so what it selects is the plugin's doing.
    """
    suite = tmp_path / 'suite'
    suite.mkdir()
    (suite / 'pytest.ini').write_text('[pytest]\n')
    (suite / 'test_selection.py').write_text(SELECTION_SUITE)
    env = {
        **os.environ,
        'PYTHONPATH': str(Path(__file__).parent),
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
    }
    done = process_sessions.run(
        [sys.executable, '-m', 'pytest', '-p', 'committed_stack_files', '-p', 'no:cacheprovider', '-rA', *arguments],
        cwd=suite,
        env=env,
        text=True,
        timeout=SELECTION_TIMEOUT,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return set(re.findall(r'^PASSED test_selection\.py::(\w+)', done.stdout, re.M)), done.stdout


def test_the_stack_files_marker_selects_every_case_that_reaches_the_fixture_and_no_other(tmp_path: Path) -> None:
    chosen, output = selected(tmp_path, '-m', MARKER)

    assert chosen == {'test_asks_for_it', 'test_reaches_it'}, output


def test_without_the_marker_every_case_runs(tmp_path: Path) -> None:
    chosen, output = selected(tmp_path)

    assert chosen == {'test_asks_for_it', 'test_reaches_it', 'test_reads_nothing'}, output


def test_the_suite_marks_a_case_that_asks_for_the_fixture(
    request: pytest.FixtureRequest, committed_stack_files: StackFiles
) -> None:
    """`tests/conftest.py` loads the plugin, so this case is one `-m stack_files` selects."""
    assert committed_stack_files.stacks()
    assert request.node.get_closest_marker(MARKER) is not None
