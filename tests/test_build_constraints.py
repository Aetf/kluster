"""Every package a build environment installs is held to one version, and renovate moves each.

`uv sync` builds the root project and every workspace member as an editable
install, each in a build environment uv resolves at that moment, and none of
those packages is among the ones `uv.lock` hashes. `pyproject.toml`'s
`[tool.uv] build-constraint-dependencies` pins them. This holds every
requirement of every `[build-system]` the sync builds to a constraint, each
constraint to one exact version, and each to the shape the renovate manager
that moves it reads.

What it cannot see from here is a package a build environment gains past its
`[build-system]`: a backend's own dependencies, and what a backend asks for on
top to build an editable install. Those are read off a sync's build
resolution, which needs the index, and pinned beside the rest.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name
from renovate_text import as_python_spells_it, as_renovate_spells_it

ROOT = Path(__file__).parent.parent

#: The manager's patterns, as Python writes them out: the list, then each entry
#: in it (`matchStringsStrategy: 'recursive'`).
LIST = r'build-constraint-dependencies = \[[^\]]*\]'
ENTRY = r'"(?<depName>[A-Za-z0-9][A-Za-z0-9._-]*)==(?<currentValue>[^"]+)"'


def built(root: Path) -> dict[Path, list[Requirement]]:
    """Each `pyproject.toml` the sync builds -- the root's and every workspace member's -- and its build requirements."""
    pyproject = tomllib.loads((root / 'pyproject.toml').read_text())
    members = pyproject.get('tool', {}).get('uv', {}).get('workspace', {}).get('members', [])
    files = [
        root / 'pyproject.toml',
        *(
            member / 'pyproject.toml'
            for pattern in members
            for member in sorted(root.glob(pattern))
            if (member / 'pyproject.toml').is_file()
        ),
    ]
    return {
        file: [
            Requirement(entry) for entry in tomllib.loads(file.read_text()).get('build-system', {}).get('requires', [])
        ]
        for file in files
    }


def constraints(root: Path) -> dict[NormalizedName, Requirement]:
    """The build constraints `pyproject.toml` declares, by the name they constrain."""
    declared = tomllib.loads((root / 'pyproject.toml').read_text())['tool']['uv'].get(
        'build-constraint-dependencies', []
    )
    return {canonicalize_name(parsed.name): parsed for parsed in map(Requirement, declared)}


def unconstrained(root: Path) -> list[tuple[str, str]]:
    """Each build requirement no constraint names: the file declaring it, and the requirement."""
    held = constraints(root)
    return [
        (file.relative_to(root).as_posix(), str(requirement))
        for file, requirements in built(root).items()
        for requirement in requirements
        if canonicalize_name(requirement.name) not in held
    ]


def test_every_build_requirement_the_sync_builds_is_constrained() -> None:
    found = built(ROOT)
    # The root and at least one member, so a walk that reached nothing is not
    # read as a tree with nothing to constrain.
    assert ROOT / 'pyproject.toml' in found
    assert len(found) > 1
    assert unconstrained(ROOT) == []


def test_a_build_requirement_added_without_a_constraint_is_named(tmp_path: Path) -> None:
    """The case above turns red when a member gains a backend requirement nobody pinned."""
    _ = (tmp_path / 'pyproject.toml').write_text(
        '[tool.uv]\nbuild-constraint-dependencies = ["hatchling==1.0"]\n'
        '[tool.uv.workspace]\nmembers = ["sdks/*"]\n'
        '[build-system]\nrequires = ["hatchling==1.0"]\n'
    )
    member = tmp_path / 'sdks' / 'one'
    member.mkdir(parents=True)
    _ = (member / 'pyproject.toml').write_text('[build-system]\nrequires = ["setuptools>=61.0", "hatchling"]\n')

    assert unconstrained(tmp_path) == [('sdks/one/pyproject.toml', 'setuptools>=61.0')]


def test_every_constraint_is_one_exact_version() -> None:
    held = constraints(ROOT)
    assert held
    for name, constraint in held.items():
        (specifier,) = constraint.specifier
        assert (specifier.operator, '*' in specifier.version) == ('==', False), name
        assert (constraint.marker, constraint.extras) == (None, set()), name


def test_the_renovate_manager_reads_every_constraint() -> None:
    """Each entry is one the manager's second pattern finds inside its first, and nothing else is."""
    config = (ROOT / 'renovate.json5').read_text()
    assert config.count(as_renovate_spells_it(LIST)) == 1
    assert config.count(as_renovate_spells_it(ENTRY)) == 1

    (block,) = as_python_spells_it(LIST).findall((ROOT / 'pyproject.toml').read_text())
    read = {
        canonicalize_name(found.group('depName')): found.group('currentValue')
        for found in as_python_spells_it(ENTRY).finditer(block)
    }

    assert read == {name: next(iter(constraint.specifier)).version for name, constraint in constraints(ROOT).items()}
