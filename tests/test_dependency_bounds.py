"""Every requirement of a package from outside this repository admits one compatible line and no more.

`pyproject.toml` bounds each such requirement below the first release past
the line of the release `uv.lock` holds: its next major, or for a 0.x release
its next minor, the line SemVer gives a 0.x release. Lock file maintenance
relocks inside the declared ranges, so a requirement left open above lets a
new major into the lock with the routine relock, which no one reads as a
major; bounded, a release past the line reaches the lock only through a pull
request that moves the bound.

The requirements are every one the project declares -- `[project]
dependencies`, its optional dependencies, and every dependency group -- save
the workspace members `[tool.uv.sources]` names, which are this repository's
own packages and carry no range. Which release is held is read from
`uv.lock`, and whether a range admits a release from `packaging`, the
reading of PEP 440 that pip and uv's metadata share.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).parent.parent


def past_the_line(version: Version) -> Version:
    """The first release past `version`'s compatible line: its next major, or for a 0.x release its next minor."""
    major, minor = (*version.release, 0)[:2]
    return Version(f'{major + 1}') if major else Version(f'0.{minor + 1}')


def requirements(pyproject: dict[str, Any]) -> list[Requirement]:
    """Every requirement the project declares, save its workspace members."""
    project = pyproject.get('project', {})
    declared: list[str] = [
        *project.get('dependencies', []),
        *(entry for extra in project.get('optional-dependencies', {}).values() for entry in extra),
        # A group's entry is a requirement or an `include-group` table, which
        # names another group read here in its own right.
        *(
            entry
            for group in pyproject.get('dependency-groups', {}).values()
            for entry in group
            if isinstance(entry, str)
        ),
    ]
    sources = pyproject.get('tool', {}).get('uv', {}).get('sources', {})
    members = {canonicalize_name(name) for name, source in sources.items() if source.get('workspace')}
    return [parsed for parsed in map(Requirement, declared) if canonicalize_name(parsed.name) not in members]


def open_past_the_line(pyproject: dict[str, Any], locked: dict[str, Version]) -> list[tuple[str, Version]]:
    """Each requirement that admits the first release past the line of the release locked for it, with that release."""
    found: list[tuple[str, Version]] = []
    for requirement in requirements(pyproject):
        past = past_the_line(locked[canonicalize_name(requirement.name)])
        if requirement.specifier.contains(past):
            found.append((requirement.name, past))
    return found


def _locked() -> dict[str, Version]:
    lock = tomllib.loads((ROOT / 'uv.lock').read_text())
    return {canonicalize_name(package['name']): Version(package['version']) for package in lock['package']}


def test_every_requirement_from_outside_the_repository_is_held_to_its_locked_line() -> None:
    pyproject = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    held = requirements(pyproject)

    # Not vacuous: the project declares such requirements, and the census
    # reads every one it holds a lock for.
    assert held
    open_above = open_past_the_line(pyproject, _locked())
    assert open_above == [], '\n'.join(f'{name} admits {past}, past its locked line' for name, past in open_above)


@pytest.mark.parametrize(
    ('version', 'past'),
    [('5.1.0', '6'), ('3.267.0', '4'), ('4.34.2', '5'), ('0.8.1', '0.9'), ('0.19.1', '0.20'), ('2.15', '3')],
)
def test_the_line_ends_at_the_next_major_or_for_a_0x_release_the_next_minor(version: str, past: str) -> None:
    assert past_the_line(Version(version)) == Version(past)


def test_a_requirement_open_above_its_line_is_named_and_a_workspace_member_is_not() -> None:
    pyproject = {
        'project': {
            'dependencies': ['floor>=4.20', 'wide>=4.20,<6', 'held>=5.1,<6', 'pinned==4.34.2', 'own'],
            'optional-dependencies': {'extra': ['zero>=0.8.1,<1']},
        },
        'dependency-groups': {'dev': ['tool>=0.16,<0.17', 'loose>=1.0', {'include-group': 'other'}]},
        'tool': {'uv': {'sources': {'own': {'workspace': True}}}},
    }
    locked = {
        name: Version(version)
        for name, version in [
            ('floor', '5.1.0'),
            ('wide', '4.30.0'),
            ('held', '5.1.0'),
            ('pinned', '4.34.2'),
            ('own', '1.0.0'),
            ('zero', '0.8.1'),
            ('tool', '0.16.9'),
            ('loose', '1.2.0'),
        ]
    }

    assert open_past_the_line(pyproject, locked) == [
        ('floor', Version('6')),
        ('wide', Version('5')),
        ('zero', Version('0.9')),
        ('loose', Version('2')),
    ]
