"""The committed checkpoints, held to what a run's own checks hold them to, after the push.

A stack whose state is committed is checked on the workstation before its
checkpoint can be pushed (framework/pulumi.md §3.3): nothing but that run
stands between the file and the public. This suite is the second look, over
every checkpoint the tree carries: it cannot stop a publication, since the
branch is public before CI runs, but it makes one loud enough to rotate after.

Only the check that needs no value runs here (`checkpoint.undeclared`): every
property the engine marks secret is ciphertext, in each resource the file
records and in each operation it holds pending — a key a resource's
`additionalSecretOutputs` names, as an output and as an input, and an output
whose input of the same name holds ciphertext anywhere, read down through
objects as the engine reads them. The other check, no secret value in the
clear, needs the stack's secrets in the clear, which CI does not hold.

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
    """Every place a checkpoint of `root` holds in the clear a property the engine marks secret, by file."""
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


def _lay_out(root: Path, resources: list[dict[str, Any]], pending: list[dict[str, Any]] | None = None) -> Path:
    """A checkpoint of `probe` in `root`, as `pulumi` lays one out, with `pending` as operations under way."""
    path = root / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks' / PROJECT / f'{STACK}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    latest: dict[str, Any] = {'manifest': {}, 'resources': resources}
    if pending:
        latest['pending_operations'] = [{'resource': resource, 'type': 'creating'} for resource in pending]
    document: dict[str, Any] = {'version': 3, 'checkpoint': {'stack': STACK, 'latest': latest}}
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
        (
            {'urn': 'urn:key', 'additionalSecretOutputs': ['keyId'], 'inputs': {'keyId': 'k'}, 'outputs': {}},
            'urn:key inputs.keyId',
        ),
        (
            {'urn': 'urn:box', 'inputs': {'spec': _ciphertext()}, 'outputs': {'spec': {'user': 'u'}}},
            'urn:box outputs.spec',
        ),
        (
            # Both rules find the one place, and it is named once.
            {
                'urn': 'urn:key',
                'additionalSecretOutputs': ['keyId'],
                'inputs': {'keyId': _ciphertext()},
                'outputs': {'keyId': 'k'},
            },
            'urn:key outputs.keyId: is named in additionalSecretOutputs',
        ),
        (
            # The form `stack export --show-secrets` writes, which the engine
            # loads from a checkpoint too.
            {
                'urn': 'urn:key',
                'inputs': {},
                'outputs': {'key': {checkpoint.SECRET_SIG: checkpoint.SECRET_MARK, 'plaintext': '"k"'}},
            },
            'urn:key outputs.key: is a secret envelope holding its plaintext',
        ),
        (
            {
                'urn': 'urn:box',
                'inputs': {'deep': {'a': _ciphertext(), 'b': 'plain'}},
                'outputs': {'deep': {'a': 'secret', 'b': 'plain'}},
            },
            'urn:box outputs.deep.a',
        ),
        (
            {'urn': 'urn:box', 'inputs': {'box': {'keys': _ciphertext()}}, 'outputs': {'box': {'keys': ['k1', 'k2']}}},
            'urn:box outputs.box.keys',
        ),
        (
            {'urn': 'urn:box', 'inputs': {'deep': {'a': _ciphertext()}}, 'outputs': {'deep': '{"a": "secret"}'}},
            'urn:box outputs.deep',
        ),
    ],
    ids=[
        'additional-secret-output',
        'secret-input-echoed',
        'additional-secret-output-input',
        'secret-object-input-echoed',
        'both-rules-at-one-place',
        'envelope-holding-its-plaintext',
        'inside-an-object',
        'array-inside-an-object',
        'object-returned-as-a-string',
    ],
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


def test_an_operation_left_pending_is_read_like_a_resource(tmp_path: Path) -> None:
    path = _lay_out(
        tmp_path,
        [],
        pending=[{'urn': 'urn:box', 'inputs': {'metadata': _ciphertext()}, 'outputs': {'metadata': 'ignition'}}],
    )

    found = findings(tmp_path)

    assert len(found) == 1
    assert found[0].startswith(f'{path.relative_to(tmp_path)}: pending_operations[0] urn:box outputs.metadata: ')


def test_a_checkpoint_holding_its_secrets_as_ciphertext_passes(tmp_path: Path) -> None:
    _ = _lay_out(
        tmp_path,
        [
            {
                'urn': 'urn:box',
                'additionalSecretOutputs': ['keyId'],
                'inputs': {
                    'metadata': _ciphertext(),
                    'keyId': _ciphertext(),
                    'deep': {'a': _ciphertext(), 'b': 'plain'},
                    'unset': _ciphertext(),
                },
                'outputs': {
                    'metadata': _ciphertext(),
                    'keyId': _ciphertext(),
                    'deep': {'a': _ciphertext(), 'b': 'plain'},
                    'address': '192.0.2.1',
                    # A null holds no value to publish.
                    'unset': None,
                },
            }
        ],
    )

    assert committed_checkpoints(tmp_path)
    assert findings(tmp_path) == []


#: The `file://` backend's directory, under the writers' own name for the
#: checkpoints directory; the segments below it are Pulumi's.
BACKEND = f'{stack_environment.CHECKPOINTS}/.pulumi'
#: What a `file://` backend writes beside a checkpoint that never leaves the
#: machine, and the driver's record of a failed check.
LOCAL = (
    f'{BACKEND}/stacks/{PROJECT}/{STACK}.json.bak',
    f'{BACKEND}/history/{PROJECT}/{STACK}/{STACK}-1.checkpoint.json',
    f'{BACKEND}/history/{PROJECT}/{STACK}/{STACK}-1.history.json',
    f'{BACKEND}/backups/{PROJECT}/{STACK}/{STACK}.1.json',
    f'{BACKEND}/locks/organization/{PROJECT}/{STACK}/lock.json',
    checkpoint.record(Path(), STACK).as_posix(),
)
#: The two files that travel.
TRAVELS = (
    f'{BACKEND}/meta.yaml',
    f'{BACKEND}/stacks/{PROJECT}/{STACK}.json',
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
