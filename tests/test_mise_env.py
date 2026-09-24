"""Which layer answers `mise.toml`'s `[env]`: the checkout's slot, or the caller.

`mise.toml` resolves what a `pulumi` run needs from the workstation slots under
`.credentials/` and, where a slot is absent, from the environment it was
started in (credentials.md §1 rule 6). Nothing else may answer: a location
outside the checkout would outrank an exported value in exactly the checkout
that has no slot, which is where a scratch probe exports a `file://` backend
of its own (framework/testing.md §5.1).

The templates are run by mise itself, not re-implemented here. The `[env]`
table is lifted into a scratch checkout outside any repository, so no parent
`mise.toml` joins in, under a `HOME` of its own, so no global configuration
does either; the values are literal on purpose, because which layer answered
is the subject.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

MISE = shutil.which('mise')
needs_mise = pytest.mark.skipif(MISE is None, reason='mise is not on PATH')

MISE_TOML = Path(__file__).resolve().parents[1] / 'mise.toml'

#: What the caller exported: a scratch backend and a passphrase of its own.
EXPORTED = {
    'PULUMI_BACKEND_URL': 'file:///scratch/state',
    'PULUMI_CONFIG_PASSPHRASE': 'exported-passphrase',
    'PGSSLROOTCERT': '/exported/ca.crt',
    'PGSSLCERT': '/exported/client.crt',
    'PGSSLKEY': '/exported/client.key',
}


def _checkout(root: Path) -> Path:
    """A directory holding `mise.toml`'s `[env]` table and nothing else."""
    table = tomllib.loads(MISE_TOML.read_text())['env']
    lines = ['[env]']
    for name, template in table.items():
        assert isinstance(template, str), f'{name} is not a template string'
        # A JSON string is a valid TOML basic string.
        lines.append(f'{name} = {json.dumps(template)}')
    checkout = root / 'checkout'
    checkout.mkdir()
    _ = (checkout / 'mise.toml').write_text('\n'.join(lines) + '\n')
    return checkout


def _resolve(checkout: Path, home: Path) -> dict[str, str]:
    """What a process `mise x` starts in `checkout` sees, given `EXPORTED`."""
    assert MISE is not None
    result = subprocess.run(
        [MISE, 'x', '--', 'env', '-0'],
        cwd=checkout,
        env={
            'PATH': str(Path(MISE).parent) + ':/usr/bin:/bin',
            'HOME': str(home),
            'MISE_TRUSTED_CONFIG_PATHS': str(checkout),
            'MISE_OFFLINE': '1',
            **EXPORTED,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    pairs = (entry.split('=', 1) for entry in result.stdout.split('\0') if '=' in entry)
    return {name: value for name, value in pairs if name in EXPORTED}


@needs_mise
def test_a_checkout_without_slots_resolves_what_the_caller_exported(tmp_path: Path) -> None:
    # Copies left where these values lived before `.credentials/` -- the
    # passphrase file at the checkout's root, the client bundle under the
    # home directory -- are inert: the caller's values come through untouched.
    checkout = _checkout(tmp_path)
    home = tmp_path / 'home'
    bundle = home / '.config' / 'kluster' / 'state-backend'
    bundle.mkdir(parents=True)
    _ = (bundle / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')
    _ = (checkout / '.pulumi.secret').write_text('old-passphrase\n')

    assert _resolve(checkout, home) == EXPORTED


@needs_mise
def test_the_slot_outranks_what_the_caller_exported(tmp_path: Path) -> None:
    # The slot is the source, so in a checkout that holds one an exported
    # value redirects nothing -- and the four state-backend values come from
    # the one bundle together.
    checkout = _checkout(tmp_path)
    slots = checkout / '.credentials'
    bundle = slots / 'state-backend'
    bundle.mkdir(parents=True)
    _ = (bundle / 'backend-url').write_text('postgres://operator@192.0.2.10:5432/pulumi_state\n')
    _ = (slots / 'pulumi.passphrase').write_text('slot-passphrase\n')

    assert _resolve(checkout, tmp_path / 'home') == {
        'PULUMI_BACKEND_URL': 'postgres://operator@192.0.2.10:5432/pulumi_state',
        'PULUMI_CONFIG_PASSPHRASE': 'slot-passphrase',
        'PGSSLROOTCERT': str(bundle / 'ca.crt'),
        'PGSSLCERT': str(bundle / 'client.crt'),
        'PGSSLKEY': str(bundle / 'client.key'),
    }
