"""Renovate's mise is the one CI runs, so every `mise.lock` change renovate writes is one CI's install reads.

`mise.toml` declares its tools locked, so CI's install refuses a pin
`mise.lock` holds no entry for, and on a renovate branch that entry is
written by the mise manager's artifact step, `mise lock <tool>`. Unconstrained,
that step runs the newest mise release on the day renovate runs, whatever it
writes and whether it may add an entry under `[tool_config] locked` at all.
`renovate.json5` constrains it to the mise every workflow hands
`jdx/mise-action`. Renovate reads the constraint from the default branch, so
the two part only on the pull request that moves mise: the one the group of
the workflows' pin opens, which moves no `mise.toml` pin and so writes no
lock. The constraint rides in that group, so one release moves both. That
pull request is where a new mise first reads entries earlier ones wrote, and
a refusal there is answered by `mise lock` with the new mise onto it.
"""

from __future__ import annotations

import re
from pathlib import Path

from renovate_text import as_python_spells_it, constraints, group, listed, package_rules, scalar
from workflow_files import mise_action_versions

ROOT = Path(__file__).parent.parent


def test_renovates_mise_is_the_one_every_workflow_installs() -> None:
    constrained = constraints((ROOT / 'renovate.json5').read_text()).get('mise', 'unconstrained, so the newest release')
    versions = mise_action_versions()

    # A census that found no step would hold nothing.
    assert versions
    assert versions == dict.fromkeys(versions, constrained), f"renovate's mise is {constrained}"


def test_the_constraint_moves_with_the_workflows_pin_as_a_bare_version() -> None:
    """The rule for the constraint's dependency puts it in the workflows' pin's pull request and drops the tag's `v`.

    Apart from the group of the workflows' pin, a mise release moves one pin
    and not the other, and the test above fails on either branch. A release
    read with its `v` would be written into the constraint as the tag, which
    is not the version the workflows pass.
    """
    config = (ROOT / 'renovate.json5').read_text()
    constrained = constraints(config)['mise']
    rules = package_rules(config)

    (constraint,) = [
        rule
        for rule in rules
        if listed(rule, 'matchManagers') == ['renovate-config'] and listed(rule, 'matchPackageNames') == ['jdx/mise']
    ]
    (workflows_pin,) = [rule for rule in rules if 'jdx/mise' in listed(rule, 'matchDepNames')]
    assert len(group(workflows_pin)) == 2
    assert group(constraint) == group(workflows_pin)

    extract = scalar(constraint, 'extractVersion')
    assert extract is not None
    # JSON5 single-quoted, so a backslash would be doubled.
    tag = as_python_spells_it(extract.replace('\\\\', '\\')).fullmatch(f'v{constrained}')
    assert tag is not None
    assert tag.group('version') == constrained


def _branch(rule: str) -> str | None:
    """The branch slug a rule puts its dependencies on: its `groupSlug`, or renovate's slug of its `groupName`."""
    slug = scalar(rule, 'groupSlug')
    name = scalar(rule, 'groupName')
    if slug is None and name is not None:
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug


def test_the_pull_request_moving_mise_moves_nothing_but_mise() -> None:
    """Every rule that puts a dependency on the branch the workflows' pin moves on matches only mise's own pins.

    Renovate writes a branch's `mise.lock` with the constraint read from the
    default branch, so on the branch moving mise it is the previous mise. A
    `mise.toml` pin on that branch would get its lock entry from the previous
    mise and its install from the new one. So the rules that name the branch,
    by slug or by the name renovate slugs, are held to two shapes: mise's own
    names alone, the workflows' pin and the action; or the constraint, the
    package `jdx/mise` of the `renovate-config` manager alone. Any other
    matcher -- a manager, a data source, a file, another name -- could reach
    a `mise.toml` pin.
    """
    rules = package_rules((ROOT / 'renovate.json5').read_text())

    (workflows_pin,) = [rule for rule in rules if 'jdx/mise' in listed(rule, 'matchDepNames')]
    branch = _branch(workflows_pin)
    assert branch is not None
    on_branch = [rule for rule in rules if _branch(rule) == branch]

    for rule in on_branch:
        keys = re.findall(r'^\s*(\w+):', rule, re.MULTILINE)
        matchers = {key: listed(rule, key) for key in keys if key.startswith(('match', 'exclude'))}
        names = set(matchers.get('matchDepNames', []))
        mise_names = set(matchers) == {'matchDepNames'} and names <= {'jdx/mise-action', 'jdx/mise'}
        constraint = matchers == {'matchManagers': ['renovate-config'], 'matchPackageNames': ['jdx/mise']}
        assert mise_names or constraint, f"a rule moving {matchers} rides on mise's branch, {branch}"
