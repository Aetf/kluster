"""The pull-request description's form (dispatch.md §1.1), as `prbody.lint` reads it.

Each case starts from one description that holds the form and breaks one
part of it, so a case names the one thing `lint` must refuse; the hook that
calls `lint` before `gh pr create` runs is `tests/test_dispatch_hooks.py`'s.
"""

from __future__ import annotations

import pytest

from kluster.scripts.dispatch import prbody

ISSUE = 922

_HOLDS = """\
Closes Aetf/kluster-ops#922

## What this is

The blog's component.

## Live

Unproven live: the first `pulumi up` of `apps` confirms the blog's route answers.

## Fails without

- Dropping the route reddens `test_the_blog_serves_its_site`.

## Sweep

The sweep read 612 tracked files, whitespace collapsed, under `re.S | re.I`:

- `blog.{0,120}route`. Positive control: "the blog's route".

## Gate

- `ruff check`, `ruff format --check`: clean.
"""


def test_a_description_in_the_form_holds() -> None:
    assert prbody.lint(_HOLDS, issue=ISSUE, closes=True) == []


@pytest.mark.parametrize(
    ('old', 'new', 'names'),
    [
        pytest.param('Closes Aetf/kluster-ops#922', 'Refs Aetf/kluster-ops#78', 'Closes', id='refers elsewhere'),
        pytest.param('## Live', '## Liveness', '## Live', id='no live section'),
        pytest.param('Unproven live: the first', 'The first', 'Unproven live:', id='live in no form'),
        pytest.param('`test_the_blog_serves_its_site`', 'the blog case', 'Fails without', id='names no test'),
        pytest.param('612 tracked files', 'the tracked files', 'files it read', id='sweep counts no files'),
        pytest.param('Positive control: "the blog\'s route"', '', 'positive control', id='sweep has no control'),
        pytest.param('- `ruff check`, `ruff format --check`: clean.', '', '## Gate', id='empty gate'),
    ],
)
def test_a_description_out_of_form_is_refused_by_what_it_lacks(old: str, new: str, names: str) -> None:
    assert old in _HOLDS
    problems = prbody.lint(_HOLDS.replace(old, new), issue=ISSUE, closes=True)
    assert len(problems) == 1
    assert names in problems[0]


@pytest.mark.parametrize(
    ('closing', 'closes', 'holds'),
    [
        pytest.param('Closes: Aetf/kluster-ops#922', True, True, id='with a colon, closing'),
        pytest.param('fixes Aetf/kluster-ops#922', True, True, id='another keyword, closing'),
        pytest.param('Closes: Aetf/kluster-ops#922\n\nRefs Aetf/kluster-ops#922', False, False, id='closes a partial'),
    ],
)
def test_every_form_github_closes_an_issue_by_is_read_as_closing(closing: str, closes: bool, holds: bool) -> None:
    body = _HOLDS.replace('Closes Aetf/kluster-ops#922', closing)
    assert (prbody.lint(body, issue=ISSUE, closes=closes) == []) is holds


def test_a_partial_dispatch_refers_to_its_issue_and_does_not_close_it() -> None:
    refers = _HOLDS.replace('Closes Aetf/kluster-ops#922', 'Refs Aetf/kluster-ops#922')
    assert prbody.lint(refers, issue=ISSUE, closes=False) == []
    assert prbody.lint(_HOLDS, issue=ISSUE, closes=False) != []


@pytest.mark.parametrize(
    'live',
    [
        pytest.param('Not provider-facing: documentation only.', id='not provider-facing'),
        pytest.param('The drill, at the head:\n\n```\n# rotate\nPASSED\n```', id='transcript'),
    ],
)
def test_the_live_section_takes_its_other_forms(live: str) -> None:
    body = _HOLDS.replace("Unproven live: the first `pulumi up` of `apps` confirms the blog's route answers.", live)
    assert prbody.lint(body, issue=ISSUE, closes=True) == []


def test_a_file_count_may_name_the_file_types_it_read() -> None:
    body = _HOLDS.replace('612 tracked files', '589 tracked `*.md`, `*.py` and `*.toml` files')
    assert prbody.lint(body, issue=ISSUE, closes=True) == []


def test_a_change_owing_no_test_says_why() -> None:
    body = _HOLDS.replace(
        '- Dropping the route reddens `test_the_blog_serves_its_site`.', 'No test: the change is prose only.'
    )
    assert prbody.lint(body, issue=ISSUE, closes=True) == []


def test_a_heading_inside_a_fenced_block_is_not_a_section() -> None:
    fenced = _HOLDS.replace('## Gate\n', '```\n## Gate\n```\n')
    assert any('no `## Gate` section' in p for p in prbody.lint(fenced, issue=ISSUE, closes=True))
