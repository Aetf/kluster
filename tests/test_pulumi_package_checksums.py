"""`Pulumi.yaml`'s `packages:` block cannot record a hash, at the pinned CLI.

The block's comment says why its pins carry no sum of what they download:
Pulumi reads a `checksums` map on a package entry but declares its values as
bytes, which its YAML decoder takes from no scalar, while the project schema
refuses anything but a string, so a `Pulumi.yaml` carrying one fails to load.
That reason is a fact about one Pulumi release, and this holds it to the
release `mise.toml` pins. A release that loads such an entry fails this test,
and the block can then carry its sums -- in whichever spelling loaded.

The project is loaded by `pulumi stack ls` against a file backend under the
test's own directory, which reads `Pulumi.yaml` and nothing on the network.
The same project without the map loads, so the refusal is the map's.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

#: The sha256 of the bridge plugin's `linux-amd64` archive at the release the
#: block pins. Any 32 bytes would do: the entry is refused before anything is
#: downloaded and compared with it.
SUM = bytes.fromhex('c9b3b07cf5e7eb5c54d40a0fb02fa43024c89a6de86ea4560c6cf17295fad2b3')

#: One entry of the block's shape, with the map's line appended per case.
PROJECT = """\
name: checksums
runtime: python
packages:
  b2:
    source: terraform-provider
    version: 1.3.0
    parameters:
      - backblaze/b2
      - 0.14.0
"""

#: A `pulumi` call loads one small file; this is a hang guard, far above that.
TIMEOUT = 60


@pytest.fixture
def pulumi() -> str:
    found = shutil.which('pulumi')
    if found is None:
        pytest.fail('pulumi is not on PATH: mise.toml pins it, so run `mise install`, then the suite under `mise x`')
    return found


def load(pulumi: str, project: str, directory: Path) -> subprocess.CompletedProcess[str]:
    """Run `pulumi stack ls` on `project`, with a home and a backend of its own."""
    (directory / 'state').mkdir()
    _ = (directory / 'Pulumi.yaml').write_text(project)
    env = {
        **os.environ,
        'PULUMI_HOME': str(directory / 'home'),
        'PULUMI_BACKEND_URL': f'file://{directory / "state"}',
        'PULUMI_SKIP_UPDATE_CHECK': '1',
    }
    return subprocess.run(
        [pulumi, 'stack', 'ls'], cwd=directory, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=False
    )


def test_the_entry_without_a_map_loads(pulumi: str, tmp_path: Path) -> None:
    result = load(pulumi, PROJECT, tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    'value',
    [SUM.hex(), base64.b64encode(SUM).decode(), f'!!binary {base64.b64encode(SUM).decode()}'],
    ids=['hex', 'base64', 'yaml-binary'],
)
def test_an_entry_carrying_a_sum_is_refused(pulumi: str, tmp_path: Path, value: str) -> None:
    project = PROJECT + f'    checksums:\n      linux-amd64: {value}\n'

    result = load(pulumi, project, tmp_path)

    assert result.returncode != 0
    assert 'package must be either a string or a package specification object' in result.stderr
