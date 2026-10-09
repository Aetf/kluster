"""The engine semantics the `state-backend` stack rests on, held by the pinned `pulumi` itself (rfc-006 §4.3).

No mock can show what the engine does around a hook, a replacement, or an
`ignore_changes` input, so each case here runs the CLI `mise.toml` pins over
a `file://` backend and a `PULUMI_HOME` of the case's own, against a program
of stand-ins: `box` in the instance's place -- replaced on `gen`, `ign`
ignored, deleted before it is replaced, a `before_create` and a
`before_delete` hook -- and `ready` in the readiness resource's, a dynamic
resource with no `diff` whose input is the box's id, replaced on it where the
case says, with an `after_create` hook. Every provider method and every hook
appends a line to a log, so the log is the order the engine ran them in; a
hook raises when `PROBE_FAIL_<hook>` is set on the run. The project around
the program is `scratch_projects`', and every command leads a POSIX session
of its own that ends with it (`process_sessions`), so a `pulumi` stopped at
its bound takes its plugins with it.

A pin bump reruns these against the new CLI. What one release does and the
next stops doing -- the second delete of a resource a failed run left pending
replacement, which 3.257.0 makes and 3.265.0 does not -- is deliberately not
held.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
from scratch_projects import scratch_project

#: How long one case may take: its project's set-up and every command it runs
#: against the engine, set from measured durations rather than from the
#: suite's per-case bound (testing.md §8). On a four-core machine the slowest
#: case here took 9 s idle, 16 s with twice as many busy processes as cores,
#: and 21 s with four times as many. Twice `COMMAND_TIMEOUT`, so a stalled
#: command fails naming itself before this fires; a stop-loss, which nothing
#: asserts on.
CASE_TIMEOUT = 240

pytestmark = [
    pytest.mark.skipif(
        shutil.which('pulumi') is None or shutil.which('uv') is None,
        reason='the pinned pulumi CLI or uv is not on PATH',
    ),
    pytest.mark.timeout(CASE_TIMEOUT),
]

PROGRAM = """\
import json
import os
import pathlib
import uuid

import pulumi
from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider, UpdateResult

LOG = pathlib.Path(os.environ['PROBE_LOG'])


def log(line):
    with LOG.open('a') as handle:
        handle.write(line + '\\n')


class BoxProvider(ResourceProvider):
    def create(self, props):
        log(f"create box gen={props['gen']} ign={props['ign']}")
        return CreateResult(id_=f"box-{props['gen']}-{uuid.uuid4().hex[:8]}", outs=dict(props))

    def diff(self, _id, olds, news):
        replaces = ['gen'] if olds.get('gen') != news.get('gen') else []
        changed = bool(replaces) or olds.get('ign') != news.get('ign')
        return DiffResult(changes=changed, replaces=replaces, delete_before_replace=True)

    def update(self, _id, _olds, news):
        log(f"update box gen={news['gen']}")
        return UpdateResult(outs=dict(news))

    def delete(self, id_, _props):
        log(f'delete box {id_}')


class ReadyProvider(ResourceProvider):
    def create(self, props):
        log(f"create ready iid={props['iid']}")
        return CreateResult(id_=f'ready-{uuid.uuid4().hex[:8]}', outs=dict(props))

    def update(self, _id, _olds, news):
        log(f"update ready iid={news['iid']}")
        return UpdateResult(outs=dict(news))

    def delete(self, id_, _props):
        log(f'delete ready {id_}')


class Box(Resource):
    def __init__(self, name, gen, ign, opts):
        super().__init__(BoxProvider(), name, {'gen': gen, 'ign': ign}, opts)


class Ready(Resource):
    def __init__(self, name, iid, opts):
        super().__init__(ReadyProvider(), name, {'iid': iid}, opts)


def hook(kind):
    def run(args):
        log(f'hook {kind} {args.name} id={args.id}')
        if os.environ.get(f'PROBE_FAIL_{kind}'):
            raise Exception(f'hook {kind} refuses')

    return pulumi.ResourceHook(kind, run)


settings = json.loads(pathlib.Path('settings.json').read_text())
if settings['box']:
    box = Box(
        'box',
        settings['gen'],
        settings['ign'],
        pulumi.ResourceOptions(
            ignore_changes=['ign'],
            delete_before_replace=True,
            hooks=pulumi.ResourceHookBinding(before_create=[hook('before_create')], before_delete=[hook('before_delete')]),
        ),
    )
    Ready(
        'ready',
        box.id,
        pulumi.ResourceOptions(
            replace_on_changes=['iid'] if settings['replace_on_changes'] else [],
            hooks=pulumi.ResourceHookBinding(after_create=[hook('after_create')]),
        ),
    )
elif settings['delete_hook']:
    hook('before_delete')
"""

#: How long one `pulumi` command may take before the case fails naming it: a
#: stop-loss, about six times the slowest whole case measured with four times
#: as many busy processes as cores (`CASE_TIMEOUT`), and below that case bound.
COMMAND_TIMEOUT = 120

STACK = 'probe'


@dataclass
class Engine:
    """One scratch project and its backend, and the commands a case runs against it."""

    project: Path
    env: dict[str, str]
    log_file: Path

    def settings(
        self,
        *,
        gen: str = 'g1',
        ign: str = 'i1',
        box: bool = True,
        replace_on_changes: bool = True,
        delete_hook: bool = True,
    ) -> None:
        _ = (self.project / 'settings.json').write_text(
            json.dumps(
                {
                    'gen': gen,
                    'ign': ign,
                    'box': box,
                    'replace_on_changes': replace_on_changes,
                    'delete_hook': delete_hook,
                }
            )
        )

    def pulumi(self, *args: str, fail: tuple[str, ...] = ()) -> sp.CompletedProcess[str]:
        """One `pulumi` command against this case's backend, with each hook named in `fail` raising."""
        env = self.env | {f'PROBE_FAIL_{kind}': '1' for kind in fail}
        return process_sessions.run(
            ['pulumi', '--non-interactive', *args], cwd=self.project, env=env, text=True, timeout=COMMAND_TIMEOUT
        )

    def up(self, *args: str, fail: tuple[str, ...] = ()) -> sp.CompletedProcess[str]:
        return self.pulumi('up', '--yes', '--skip-preview', *args, fail=fail)

    def log(self) -> list[str]:
        """What ran since the last read, in order, and nothing before it."""
        lines = self.log_file.read_text().splitlines() if self.log_file.exists() else []
        self.log_file.unlink(missing_ok=True)
        return lines

    def resources(self) -> dict[str, dict[str, Any]]:
        """The state's resources by logical name, the pending replacements among them."""
        exported = self.pulumi('stack', 'export')
        assert exported.returncode == 0, exported.stderr
        deployment = cast('dict[str, Any]', json.loads(exported.stdout))['deployment']
        found = cast('list[dict[str, Any]]', deployment.get('resources') or [])
        return {str(resource['urn']).rsplit('::', 1)[1]: resource for resource in found}

    def urn(self, name: str) -> str:
        return str(self.resources()[name]['urn'])


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    log_file = tmp_path / 'engine.log'
    scratch = scratch_project(tmp_path, PROGRAM)
    made = Engine(project=scratch.directory, env=scratch.env | {'PROBE_LOG': str(log_file)}, log_file=log_file)
    # The backend in hand is the case's own, and holds nothing yet
    # (framework/testing.md §5.1).
    listed = made.pulumi('stack', 'ls', '--all', '--json')
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout) == []
    initialized = made.pulumi('stack', 'init', STACK)
    assert initialized.returncode == 0, initialized.stderr
    made.settings()
    return made


@pytest.fixture
def launched(engine: Engine) -> Engine:
    """`engine` after its first `up`: the stand-in and its dependent exist, and the log is empty."""
    first = engine.up(fail=())
    assert first.returncode == 0, first.stdout + first.stderr
    _ = engine.log()
    return engine


def _ran(result: sp.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_a_passing_before_delete_runs_before_the_delete_and_the_dependent_is_replaced_with_it(
    launched: Engine,
) -> None:
    old = launched.resources()['box']['id']
    launched.settings(gen='g2')

    result = launched.up()

    assert result.returncode == 0, _ran(result)
    new = launched.resources()['box']['id']
    # Delete before replace: the dependent goes first, then the hook, then the
    # old box, and only then the new box, its dependent and that one's hook.
    assert [re.sub(r'ready-[0-9a-f]{8}', 'ready-<id>', line) for line in launched.log()] == [
        'delete ready ready-<id>',
        f'hook before_delete box id={old}',
        f'delete box {old}',
        'hook before_create box id=',
        'create box gen=g2 ign=i1',
        f'create ready iid={new}',
        'hook after_create ready id=ready-<id>',
    ]


def test_a_raising_before_delete_leaves_the_stand_in_and_fails_the_run(launched: Engine) -> None:
    old = launched.resources()['box']['id']
    launched.settings(gen='g2')

    result = launched.up(fail=('before_delete',))

    assert result.returncode != 0
    assert 'hook before_delete refuses' in _ran(result)
    log = launched.log()
    assert f'hook before_delete box id={old}' in log
    assert not [line for line in log if line.startswith(('delete box', 'create box'))]
    assert launched.resources()['box']['id'] == old


def test_a_refused_replacement_leaves_the_dependent_to_be_re_created_against_the_unreplaced_box(
    launched: Engine,
) -> None:
    # Slice 0's X1: the dependent was deleted before the hook refused, and the
    # next run re-creates it and runs its `after_create`, with the change
    # withdrawn and the box never replaced. The restore hook meets a box that
    # holds every stack there, and must pass.
    old = launched.resources()['box']['id']
    launched.settings(gen='g2')
    refused = launched.up(fail=('before_delete',))
    assert refused.returncode != 0
    assert launched.resources()['ready'].get('pendingReplacement') is True
    _ = launched.log()
    launched.settings(gen='g1')

    result = launched.up()

    assert result.returncode == 0, _ran(result)
    log = launched.log()
    assert f'create ready iid={old}' in log
    assert [line for line in log if line.startswith('hook after_create ready')]
    assert not [line for line in log if line.startswith(('create box', 'update box', 'delete box'))]
    assert launched.resources()['box']['id'] == old


def test_a_raising_before_create_leaves_the_stand_in_uncreated(engine: Engine) -> None:
    result = engine.up(fail=('before_create',))

    assert result.returncode != 0
    assert 'hook before_create refuses' in _ran(result)
    assert 'create box gen=g1 ign=i1' not in engine.log()
    assert 'box' not in engine.resources()


def test_a_preview_runs_no_hook(launched: Engine) -> None:
    launched.settings(gen='g2')

    previewed = launched.pulumi('preview')

    assert previewed.returncode == 0, _ran(previewed)
    assert not [line for line in launched.log() if line.startswith('hook')]


def test_a_raising_after_create_leaves_its_resource_recorded_and_fails_the_run(engine: Engine) -> None:
    # The SDK's own docstring for `ResourceHookBinding` says an after hook
    # that raises only warns.
    result = engine.up(fail=('after_create',))

    assert result.returncode != 0
    assert 'hook after_create refuses' in _ran(result)
    recorded = engine.resources()
    assert 'ready' in recorded
    assert not recorded['ready'].get('pendingReplacement')


def test_a_replacement_is_created_from_the_programs_value_of_an_ignored_input(launched: Engine) -> None:
    launched.settings(ign='i2')
    unchanged = launched.up()
    assert unchanged.returncode == 0, _ran(unchanged)
    assert not [line for line in launched.log() if line.startswith(('create', 'update', 'delete'))]
    launched.settings(gen='g2', ign='i2')

    result = launched.up()

    assert result.returncode == 0, _ran(result)
    assert 'create box gen=g2 ign=i2' in launched.log()


def test_a_targeted_replacement_is_created_from_the_programs_value_of_an_ignored_input(launched: Engine) -> None:
    box = launched.urn('box')
    launched.settings(ign='i3')

    result = launched.up('--target-replace', box, '--target-dependents')

    assert result.returncode == 0, _ran(result)
    assert 'create box gen=g1 ign=i3' in launched.log()


def test_a_dependent_without_replace_on_changes_is_updated_and_runs_no_after_create(engine: Engine) -> None:
    # Why the readiness resource declares `replace_on_changes` on the
    # instance's id: with no `diff` of its own, a new id is an update.
    engine.settings(replace_on_changes=False)
    first = engine.up()
    assert first.returncode == 0, _ran(first)
    _ = engine.log()
    engine.settings(gen='g2', replace_on_changes=False)

    result = engine.up()

    assert result.returncode == 0, _ran(result)
    log = engine.log()
    assert [line for line in log if line.startswith('update ready')], log
    assert not [line for line in log if line.startswith('hook after_create')]


def test_destroy_without_running_the_program_refuses_a_resource_with_a_delete_hook(launched: Engine) -> None:
    refused = launched.pulumi('destroy', '--yes')

    assert refused.returncode != 0
    assert '--run-program' in _ran(refused)
    assert 'box' in launched.resources()

    ran = launched.pulumi('destroy', '--yes', '--run-program')

    assert ran.returncode == 0, _ran(ran)
    log = launched.log()
    assert [line for line in log if line.startswith('hook before_delete box')]
    assert log.index(next(line for line in log if line.startswith('hook before_delete'))) < log.index(
        next(line for line in log if line.startswith('delete box'))
    )


def test_a_delete_whose_hook_the_program_no_longer_registers_fails_with_the_box_in_place(launched: Engine) -> None:
    # The state records a hook by name, so the program registers the delete
    # hook for as long as the instance exists.
    launched.settings(box=False, delete_hook=False)

    result = launched.up()

    assert result.returncode != 0
    assert 'was not registered' in _ran(result)
    assert 'box' in launched.resources()
    assert not [line for line in launched.log() if line.startswith('delete box')]
