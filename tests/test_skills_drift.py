"""The vendored-skills comparison, without reaching the network.

What is worth pinning is the shape of the answer, since the check's job is to
tell three situations apart: our copy has been edited, upstream has moved on,
and upstream has grown a file the installer would now bring in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kluster.scripts import skills_drift


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / '.agents' / 'skills' / 'demo'
    (root / 'agents').mkdir(parents=True)
    _ = (root / 'SKILL.md').write_bytes(b'body\n')
    _ = (root / 'agents' / 'openai.yaml').write_bytes(b'name: demo\n')
    monkeypatch.setattr(skills_drift, 'INSTALLED', tmp_path / '.agents' / 'skills')
    return root


def test_identical_copies_are_no_drift(installed: Path) -> None:
    _ = installed
    upstream = {'pulumi/skills/demo/SKILL.md': b'body\n', 'pulumi/skills/demo/agents/openai.yaml': b'name: demo\n'}

    assert not skills_drift.compare('demo', upstream)


def test_a_changed_file_is_reported(installed: Path) -> None:
    _ = installed
    upstream = {
        'pulumi/skills/demo/SKILL.md': b'rewritten\n',
        'pulumi/skills/demo/agents/openai.yaml': b'name: demo\n',
    }

    drift = skills_drift.compare('demo', upstream)

    assert drift.changed == ['demo/SKILL.md']
    assert not drift.removed_upstream and not drift.added_upstream


def test_a_file_upstream_dropped_is_reported(installed: Path) -> None:
    _ = installed
    upstream = {'pulumi/skills/demo/SKILL.md': b'body\n'}

    drift = skills_drift.compare('demo', upstream)

    assert drift.removed_upstream == ['demo/agents/openai.yaml']


def test_a_new_upstream_file_is_reported(installed: Path) -> None:
    _ = installed
    upstream = {
        'pulumi/skills/demo/SKILL.md': b'body\n',
        'pulumi/skills/demo/agents/openai.yaml': b'name: demo\n',
        'pulumi/skills/demo/use_cases.yaml': b'cases: []\n',
    }

    drift = skills_drift.compare('demo', upstream)

    assert drift.added_upstream == ['demo/use_cases.yaml']


def test_files_the_installer_never_copies_are_not_drift(installed: Path) -> None:
    _ = installed
    # Upstream computes its hash over these and then drops them while
    # installing, which is why the lock's hash cannot be recomputed at all
    # (vercel-labs/skills#806). Their absence is normal, not a finding.
    upstream = {
        'pulumi/skills/demo/SKILL.md': b'body\n',
        'pulumi/skills/demo/agents/openai.yaml': b'name: demo\n',
        'pulumi/skills/demo/metadata.json': b'{}\n',
        'pulumi/skills/demo/.gitignore': b'node_modules\n',
    }

    assert not skills_drift.compare('demo', upstream)


def test_the_namespace_is_discovered_not_assumed(installed: Path) -> None:
    _ = installed
    # The lock file records the repository but not which top-level directory
    # inside it holds the skill.
    upstream = {
        'some-other-namespace/skills/demo/SKILL.md': b'body\n',
        'some-other-namespace/skills/demo/agents/openai.yaml': b'name: demo\n',
    }

    assert not skills_drift.compare('demo', upstream)


def test_a_skill_missing_upstream_is_an_error(installed: Path) -> None:
    _ = installed

    with pytest.raises(RuntimeError, match='not in the source repository'):
        _ = skills_drift.compare('demo', {'pulumi/skills/other/SKILL.md': b'x\n'})
