"""The rendering mechanism itself: what a file's name decides, and what it reads.

The templates that use it are checked where they are declared — the gateway's
unit files, the overlay's rules program, the worker's stylesheet. What is
checked here is the mechanism those all sit on (rfc-002 §9.1): where a file is
found, which files are rendered rather than copied, and what happens to a
parameter nobody supplied.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from jinja2 import UndefinedError

from kluster.lib import templates


@dataclass(frozen=True)
class Greeting:
    """A template's parameters, as every call site declares them: a dataclass."""

    who: str


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A throwaway package laid out the way a component is.

    An importable package with a `templates/` directory beside its module, so
    the anchor `importlib.resources` resolves against is a real one rather than
    a directory path this test invented.
    """
    name = 'sample'
    root = tmp_path / name
    nested = root / 'templates' / 'nested'
    nested.mkdir(parents=True)
    (root / '__init__.py').write_text('')
    (root / 'templates' / 'greeting.txt.j2').write_text('hello {{ who }}\n')
    (root / 'templates' / 'dashboard.json').write_text('{"title": "{{ who }}"}\n')
    (nested / 'inner.conf.j2').write_text('who = {{ who }}\n')

    monkeypatch.setattr(sys, 'path', [str(tmp_path), *sys.path])
    # Every case gets its own directory, so a copy imported by an earlier one
    # must not be what this one resolves against.
    _ = sys.modules.pop(name, None)
    importlib.invalidate_caches()
    yield name
    _ = sys.modules.pop(name, None)


def test_load_hands_back_the_file_with_nothing_substituted(package: str) -> None:
    """`load` is for the caller whose artefact *is* the file.

    The verbatim half of the suffix rule leans on this: a stylesheet or a
    dashboard reaches its reader as the bytes on disk, braces included.
    """
    assert templates.load(package, 'templates/greeting.txt.j2') == 'hello {{ who }}\n'


def test_render_reads_its_names_from_the_parameter_dataclass(package: str) -> None:
    assert templates.render(package, 'templates/greeting.txt.j2', Greeting(who='alice')) == 'hello alice\n'


def test_a_trailing_newline_survives_rendering(package: str) -> None:
    """A unit file and a shell script both end in one, and both are compared as text."""
    assert templates.render(package, 'templates/greeting.txt.j2', Greeting(who='alice')).endswith('\n')


def test_a_forgotten_parameter_is_refused_rather_than_rendered_empty(package: str) -> None:
    """`StrictUndefined`, so the failure is here and not in the file's reader.

    A configuration file with a blank where a name should be is accepted by
    most of the programs this repository writes for, and the mistake then
    surfaces as behaviour rather than as an error.
    """
    with pytest.raises(UndefinedError):
        templates.render(package, 'templates/greeting.txt.j2')


def test_parameters_are_a_dataclass_rather_than_a_bag_of_names(package: str) -> None:
    with pytest.raises(TypeError, match='frozen dataclass'):
        templates.render(package, 'templates/greeting.txt.j2', {'who': 'alice'})


def test_the_j2_suffix_decides_that_a_file_is_rendered_and_leaves_the_key_without_it(package: str) -> None:
    """One call for a directory holding both kinds, and no globs on either side."""
    tree = templates.render_tree(package, 'templates', Greeting(who='alice'))

    assert tree['greeting.txt'] == 'hello alice\n'
    assert 'greeting.txt.j2' not in tree


def test_a_file_without_the_suffix_is_copied_through_with_its_braces_intact(package: str) -> None:
    """What keeps a Grafana dashboard or a Go template safe by construction.

    Its `{{ … }}` belongs to whoever renders it later, so the name of the file
    is what decides — not the caller remembering to pass no parameters.
    """
    tree = templates.render_tree(package, 'templates', Greeting(who='alice'))

    assert tree['dashboard.json'] == '{"title": "{{ who }}"}\n'


def test_a_nested_file_is_keyed_by_its_path_under_the_directory(package: str) -> None:
    """Keys are relative paths, which is what a config map's keys have to be."""
    tree = templates.render_tree(package, 'templates', Greeting(who='alice'))

    assert tree['nested/inner.conf'] == 'who = alice\n'
