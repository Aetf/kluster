"""The committed checkpoints, held to what a run's own checks hold them to, after the push.

A stack whose state is committed is checked on the workstation before its
checkpoint can be pushed (framework/pulumi.md §3.3): nothing but that run
stands between the file and the public. This suite is the second look, over
every checkpoint the tree carries: it cannot stop a publication, since the
branch is public before CI runs, but it makes one loud enough to rotate after.

Only the check that needs no value runs here (`checkpoint.undeclared`): each
output a resource's `additionalSecretOutputs` names, and each output whose
input of the same name is wholly ciphertext, is ciphertext. It reads one
level: a marking lost inside an object, or an input that
`additionalSecretOutputs` names, passes it (Aetf/kluster-ops#483). The other
check, no secret value in the clear, needs the stack's secrets in the clear,
which CI does not hold.

And `.gitignore` is held to the layout: what a `file://` backend writes beside
a checkpoint and keeps on the machine stays out of every change, and the two
files that travel do not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kluster.lib import stack_environment
from kluster.scripts.operator_stack import checkpoint

ROOT = Path(__file__).parent.parent

#: The stack and project the positive controls lay a checkpoint out for.
STACK = 'probe'
PROJECT = 'kluster-py'


def committed_checkpoints(root: Path) -> list[Path]:
    """Every checkpoint a checkout carries, under whichever project directory holds it."""
    return sorted((root / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks').glob('*/*.json'))


def findings(root: Path) -> list[str]:
    """Every declared-secret property a checkpoint of `root` holds in the clear, by file and property."""
    return [
        f'{path.relative_to(root)}: {finding}'
        for path in committed_checkpoints(root)
        for finding in checkpoint.undeclared(json.loads(path.read_text()))
    ]


def test_no_committed_checkpoint_holds_a_declared_secret_in_the_clear() -> None:
    # A finding here is a value already public: the fix is the program change
    # that marks it and the next `up`, and the credential behind it rotated.
    assert findings(ROOT) == []


def _ciphertext() -> dict[str, str]:
    return {checkpoint.SECRET_SIG: checkpoint.SECRET_MARK, 'ciphertext': 'v1:c2VjcmV0'}


def _lay_out(root: Path, resources: list[dict[str, Any]]) -> Path:
    """A checkpoint of `probe` in `root`, as `pulumi` lays one out."""
    path = root / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks' / PROJECT / f'{STACK}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        'version': 3,
        'checkpoint': {'stack': STACK, 'latest': {'manifest': {}, 'resources': resources}},
    }
    _ = path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize(
    ('resource', 'named'),
    [
        (
            {'urn': 'urn:key', 'additionalSecretOutputs': ['keyId'], 'inputs': {}, 'outputs': {'keyId': 'k'}},
            'urn:key outputs.keyId',
        ),
        (
            {'urn': 'urn:box', 'inputs': {'metadata': _ciphertext()}, 'outputs': {'metadata': 'ignition'}},
            'urn:box outputs.metadata',
        ),
    ],
    ids=['additional-secret-output', 'secret-input-echoed'],
)
def test_a_checkpoint_with_a_declared_secret_in_the_clear_is_named(
    tmp_path: Path, resource: dict[str, Any], named: str
) -> None:
    """The positive control: a tree whose checkpoint leaks is found, by file and property.

    Without it the case above passes over a tree that holds no checkpoint at
    all, and over one whose checkpoint the walk never reached.
    """
    path = _lay_out(tmp_path, [resource])

    found = findings(tmp_path)

    assert len(found) == 1
    assert found[0].startswith(f'{path.relative_to(tmp_path)}: {named}')


def test_a_checkpoint_holding_its_secrets_as_ciphertext_passes(tmp_path: Path) -> None:
    _ = _lay_out(
        tmp_path,
        [
            {
                'urn': 'urn:box',
                'additionalSecretOutputs': ['keyId'],
                'inputs': {'metadata': _ciphertext()},
                'outputs': {'metadata': _ciphertext(), 'keyId': _ciphertext(), 'address': '192.0.2.1'},
            }
        ],
    )

    assert committed_checkpoints(tmp_path)
    assert findings(tmp_path) == []


#: What a `file://` backend writes beside a checkpoint that never leaves the
#: machine, and the driver's record of a failed check.
LOCAL = (
    f'checkpoints/.pulumi/stacks/{PROJECT}/{STACK}.json.bak',
    f'checkpoints/.pulumi/history/{PROJECT}/{STACK}/{STACK}-1.checkpoint.json',
    f'checkpoints/.pulumi/history/{PROJECT}/{STACK}/{STACK}-1.history.json',
    f'checkpoints/.pulumi/backups/{PROJECT}/{STACK}/{STACK}.1.json',
    f'checkpoints/.pulumi/locks/organization/{PROJECT}/{STACK}/lock.json',
    f'checkpoints/{STACK}.failed-check',
)
#: The two files that travel.
TRAVELS = (
    'checkpoints/.pulumi/meta.yaml',
    f'checkpoints/.pulumi/stacks/{PROJECT}/{STACK}.json',
)


@pytest.mark.parametrize('path', LOCAL + TRAVELS)
def test_gitignore_keeps_what_stays_on_the_machine_and_lets_the_checkpoint_through(tmp_path: Path, path: str) -> None:
    """Evaluated in a repository of its own, with no global or system configuration.

    Inside a `jj` workspace git walks up to the primary checkout and re-roots
    every path (framework/dispatch.md §1.2), and a user's own excludes file
    would answer beside this one.
    """
    env = {
        'PATH': os.environ['PATH'],
        'HOME': str(tmp_path),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
    }
    _ = subprocess.run(['git', 'init', '-q', str(tmp_path)], env=env, check=True, timeout=60)
    _ = shutil.copy(ROOT / '.gitignore', tmp_path / '.gitignore')
    (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / path).touch()

    completed = subprocess.run(
        ['git', 'check-ignore', '--no-index', '-q', path], cwd=tmp_path, env=env, check=False, timeout=60
    )

    assert completed.returncode in (0, 1)
    assert (completed.returncode == 0) == (path in LOCAL)
