"""Every `#!/bin/sh` script the gateway runs is run in the suite under the device's dash.

The device runs a `#!/bin/sh` script under dash; the host running the suite
has whichever `/bin/sh` it has, which is not the device's: bash on a
workstation, which passes a bashism that fails on the device, and a later dash
on CI's Ubuntu runners. So a case that runs one goes through
`device_shell.argv`, which names the dash `mise.toml` pins.

What is held here is that nothing is left out of that. The templates are
found by their interpreter, not by a list of names, and each is held to a
module of the suite that runs it through the runner: an `argv` call naming
the program the template is delivered as, as the command of a
`process_sessions.run` or `started` call -- written there, or unpacked into
the list that is. A template that no module runs that way, because its cases
run it through the host's shell or because no case runs it, is reported.

The program argument is read from the module's source, not by importing the
module: framework/testing.md §2 has no test module import another. It is
resolved through the module's own import statements to the `kluster` module
they name, which is the only thing imported, and an argument whose root no
import from `kluster` binds is refused by name.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

import device_shell
import pinned_tools
import process_sessions
import pytest

TESTS = Path(__file__).parent

#: What a call of the runner is spelled as in a module that imports it.
RUNNER = 'device_shell.argv'

#: The calls that start a process, whose first argument is its command.
STARTS = ('process_sessions.run', 'process_sessions.started')

#: The package whose constants a program argument names.
PRODUCTION = 'kluster'

#: Bounds one run of a two-line script.
SHELL_TIMEOUT = 10.0


def _is_runner(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and ast.unparse(node.func) == RUNNER and bool(node.args)


def _run_programs(tree: ast.Module) -> list[ast.expr]:
    """The program argument of every runner call that is the command a process starts with."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and ast.unparse(node.func) in STARTS and node.args):
            continue
        command = node.args[0]
        runners = [command] if isinstance(command, ast.Call) else []
        if isinstance(command, ast.List):
            runners = [element.value for element in command.elts if isinstance(element, ast.Starred)]
        found.extend(runner.args[0] for runner in runners if _is_runner(runner) and isinstance(runner, ast.Call))
    return found


def _bindings(tree: ast.Module) -> dict[str, str]:
    """Each name the module's import statements bind, with the dotted name it is bound to."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            bound.update({alias.asname or alias.name: f'{node.module}.{alias.name}' for alias in node.names})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split('.', 1)[0]
                bound[alias.asname or root] = alias.name if alias.asname else root
    return bound


def _dotted(expression: ast.expr) -> list[str] | None:
    """`a.b.c` as its parts, or `None` for anything but a name and attributes on it."""
    if isinstance(expression, ast.Name):
        return [expression.id]
    if isinstance(expression, ast.Attribute):
        inner = _dotted(expression.value)
        return None if inner is None else [*inner, expression.attr]
    return None


def _resolved(expression: ast.expr, bound: dict[str, str], module: Path) -> object:
    """The value a program argument names: the production module its root is imported from, then its attributes."""
    parts = _dotted(expression)
    target = bound.get(parts[0], '') if parts else ''
    if parts is None or not (target == PRODUCTION or target.startswith(f'{PRODUCTION}.')):
        pytest.fail(
            f'{module.name} runs {RUNNER} on {ast.unparse(expression)}, which no import from {PRODUCTION} binds'
        )
    path = [*target.split('.'), *parts[1:]]
    for length in range(len(path), 0, -1):
        name = '.'.join(path[:length])
        try:
            value: object = importlib.import_module(name)
        except ModuleNotFoundError as missing:
            if missing.name != name:
                raise
            continue
        for attribute in path[length:]:
            value = getattr(value, attribute)
        return value
    pytest.fail(f'{module.name} runs {RUNNER} on {ast.unparse(expression)}, and {target} imports as no module')


def programs_run_under_dash() -> set[str]:
    """Each program some module of the suite starts through the runner.

    This module is left out: the scripts its own cases run are of its own
    making, not a template's rendering.
    """
    found: set[str] = set()
    for module in sorted(TESTS.glob('*.py')):
        if module == Path(__file__):
            continue
        tree = ast.parse(module.read_text(), filename=str(module))
        programs = _run_programs(tree)
        bound = _bindings(tree) if programs else {}
        found.update(str(_resolved(program, bound, module)) for program in programs)
    return found


def test_every_device_sh_template_is_run_under_dash() -> None:
    """A `#!/bin/sh` template that no case runs through the runner is one a bashism passes in.

    The census is guarded against coming back empty: an interpreter line the
    census stopped recognizing would leave the loop nothing to visit and pass.
    """
    templates = device_shell.posix_templates()
    assert templates, f'no template under {device_shell.TEMPLATES} names {device_shell.INTERPRETER}'
    covered = programs_run_under_dash()

    missing = [template.name for template in templates if device_shell.program(template) not in covered]

    assert not missing, f'no case runs these through {RUNNER}: {missing}'


def _run(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    pinned_tools.require(device_shell.DASH)
    script = tmp_path / 'script'
    _ = script.write_text(text)
    any_program = device_shell.program(device_shell.posix_templates()[0])
    return process_sessions.run(device_shell.argv(any_program, script), text=True, timeout=SHELL_TIMEOUT)


@pytest.mark.parametrize(
    ('line', 'refused'),
    [('[[ a = a ]]', True), ('[ a = a ]', False)],
    ids=['bash-test-keyword', 'posix-test'],
)
def test_the_runner_names_a_shell_that_refuses_a_bashism(tmp_path: Path, line: str, refused: bool) -> None:
    """The shell the runner names is not bash: it refuses `[[`, which bash runs, and runs the POSIX `[`."""
    completed = _run(tmp_path, f'{device_shell.SHEBANG}\n{line}\n')

    assert (completed.returncode != 0) is refused, completed.stderr


def test_the_runner_hands_the_shell_the_options_its_first_line_names(tmp_path: Path) -> None:
    """`#!/bin/sh -eu` runs under `-e` and `-u`, as the device's kernel starts it: the failure ends the script."""
    completed = _run(tmp_path, f'{device_shell.SHEBANG} -eu\nfalse\necho reached\n')

    assert completed.returncode != 0
    assert 'reached' not in completed.stdout
