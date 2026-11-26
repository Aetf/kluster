"""`gate: enumeration`: its extractor on reviewed changes and on a brief.

Each fixture under `tests/data/gates/enumeration/` is one file of a reviewed
pull request: its text at the head the review read, the lines the pull
request wrote there, and under `program` the text at that head of every
other file the extractor reads for it. Every case of the gate's labelled set
drawn from that file is an item the extractor draws from that change, its
text and context included, since a case holds the item as the extractor
drew it (`gates.cases`); and every item is of a line the change writes. The
context a case's finding turns on is shown on the case it was missing from:
the guard around a message (#920), the docstring of the test case around an
assertion (#819), the `#:` comment above a binding (#911), and the census a
docstring names in a code span (#925).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from kluster.scripts.dispatch.gates.enumeration import extract
from kluster.scripts.dispatch.gates.extract import ITEM_CONTEXT_CAP, Change, Changed, Item
from kluster.scripts.dispatch.gates.gate import named
from kluster.scripts.dispatch.gates.reading import brief_path

DATA = Path(__file__).parent / 'data' / 'gates' / 'enumeration'
GATE = named('enumeration')


def _change(fixture: Path) -> tuple[dict[str, Any], Change]:
    """A fixture's record, and the change it holds: one file, the lines it writes, the head's text of what it reads."""
    held = cast(dict[str, Any], json.loads(fixture.read_text()))
    path = str(held['path'])
    files = {path: str(held['text']), **cast(dict[str, str], held.get('program', {}))}
    written = tuple((int(first), int(last)) for first, last in cast(list[list[int]], held['written']))
    changed = Changed(path, str(held['kind']), written)
    return held, Change(head=str(held['head']), files=(changed,), text=files.get)


def _item(fixture: str, line: int) -> Item:
    """The item the extractor draws at `line` from the change `fixture` holds."""
    _, change = _change(DATA / f'{fixture}.json')
    (item,) = [i for i in extract(change) if i.line == line]
    return item


@pytest.mark.parametrize('fixture', sorted(DATA.glob('*.json')), ids=lambda p: p.stem)
def test_each_case_of_a_reviewed_change_is_an_item_the_extractor_draws_from_it(fixture: Path) -> None:
    held, change = _change(fixture)
    drawn = extract(change)
    cases = [c for c in GATE.cases() if (c.pr, c.head, c.path) == (held['pr'], held['head'], held['path'])]

    assert cases, f'{fixture.name} holds no case of the set'
    by_place = {(i.path, i.line, i.end_line): i for i in drawn}
    assert [by_place.get((c.path, c.line, c.end_line)) for c in cases] == [c.item for c in cases]
    (changed,) = change.files
    assert all(any(i.overlaps(first, last) for first, last in changed.written) for i in drawn)


def test_a_statement_carries_the_headers_around_it_and_the_docstring_of_its_case() -> None:
    # The message names the volumes that have a binder, and the guard it is
    # raised under is where `has_binder` is tested.
    message = _item('920-k8s', 1390)
    assert '\n        if self.binder_held_to and not self.keeper.has_binder:' in message.context
    # The objects the assertion lists are the case's docstring's.
    listed = _item('819-test_etcd_snapshots', 104)
    assert "the namespace holds the snapshot's own objects alone" in listed.context


def test_a_binding_carries_the_comment_above_it() -> None:
    steps = _item('911-test_checks_job', 66)

    assert (
        '#: The steps the fast path runs: the setup, the verdict, the tests and the\n'
        '#: prose, which a change of this shape can reach (framework/ci.md §3).'
    ) in steps.context


def test_a_docstring_carries_the_programs_definition_of_what_it_names_in_a_code_span() -> None:
    paragraph = _item('925-storage', 266)

    # `conventions.backup.BACKED_NAMESPACES`, read through the module's
    # `from kluster import conventions`: the census, with the family the
    # paragraph's conditions leave out.
    assert '# src/kluster/conventions/backup.py:203–219\nBACKED_NAMESPACES' in paragraph.context
    assert 'MONITORING_NAMESPACE: frozenset({BackupFamily.VM})' in paragraph.context
    # `kluster.lib.k8s.sealed_ciphertext`, by its absolute path, and `_pair`,
    # a function of the module itself.
    assert '# src/kluster/lib/k8s.py:1100–1119\ndef sealed_ciphertext(' in paragraph.context
    assert '# src/kluster/components/apps/storage.py:717–736\ndef _pair(' in paragraph.context


BRIEF = """---
issue: 917
role: builder
done: The census holds the new component.
owned: [tests/test_census.py]
closes: true
---
# Brief: the census

Write the component. Add its name to the
census in `tests/test_census.py`, beside the others. Its placement follows
from what it declares.
"""


def test_a_brief_sentence_that_tells_the_agent_to_add_an_entry_is_drawn_at_its_lines() -> None:
    path = brief_path({'cluster'}, 'b-917.md')
    change = Change(head='0' * 64, files=(Changed(path, 'A', ((1, 14),)),), text=lambda _: BRIEF)

    drawn = extract(change)

    assert [(i.line, i.end_line, i.text) for i in drawn] == [
        (10, 11, 'Add its name to the\ncensus in `tests/test_census.py`, beside the others.')
    ]


def _written(files: dict[str, str]) -> Change:
    """A change that writes each of `files` whole."""
    changed = tuple(Changed(path, 'A', ((1, text.count('\n')),)) for path, text in files.items())
    return Change(head='0' * 40, files=changed, text=files.get)


SET = "NAMES = ('authelia', 'blog', 'immich')\n"


def test_text_held_here_but_written_elsewhere_gives_no_item() -> None:
    change = _written({'tests/data/gates/x/recorded.py': SET, 'tests/vendor/lib/mod.py': SET, 'tests/test_x.py': SET})

    assert [i.path for i in extract(change)] == ['tests/test_x.py']


def test_a_display_is_a_membership_where_a_statement_defines_or_parametrizes_a_set_with_it() -> None:
    lines = [
        'import os, sys',
        "@pytest.mark.parametrize('name', ['authelia', 'blog'])",
        'def test_a(name: str) -> None:',
        "    sp.run(['git', 'log', '--oneline'], check=True)",
        "    root.type, root.mode = 'dir', 0o755",
        "    assert name in ('authelia', 'blog')",
    ]

    drawn = extract(_written({'tests/test_x.py': '\n'.join(lines) + '\n'}))

    assert [i.line for i in drawn] == [2, 6]


def test_a_statement_under_a_clause_carries_the_clause_once_and_not_the_one_before_it() -> None:
    lines = [
        'def pick(x: int) -> None:',
        '    match x:',
        '        case 1:',
        "            first = ('a', 'b')",
        '        case 2:',
        '            # The second pair.',
        "            second = ('c', 'd')",
        '    try:',
        '        pass',
        '    except KeyError:',
        "        missing = ('e', 'f')",
        '    if x:',
        '        pass',
        '    else:',
        "        other = ('g', 'h')",
    ]

    drawn = {i.line: i.context for i in extract(_written({'tests/test_x.py': '\n'.join(lines) + '\n'}))}

    assert '    match x:\n        case 2:\n' in drawn[7]
    assert 'case 1' not in drawn[7]
    assert drawn[7].count('# The second pair.') == 1
    assert '    try:\n    except KeyError:' in drawn[11]
    assert '    if x:\n    else:' in drawn[15]


def test_a_docstring_spanning_many_large_definitions_is_held_to_the_items_bound() -> None:
    names = [f'N{n}' for n in range(80)]
    lines = [
        f'"""The names: {", ".join(f"`{name}`" for name in names[:-1])} and `{names[-1]}`."""',
        *(f"{name} = '{'x' * 1400}'" for name in names),
    ]

    (item,) = extract(_written({'tests/test_x.py': '\n'.join(lines) + '\n'}))

    # Each definition is a part of its own, capped; together they run far
    # over the bound, so the item's middle is cut, and both ends stay.
    assert len(item.context) <= ITEM_CONTEXT_CAP
    assert 'characters of the context left out' in item.context
    assert item.context.startswith("# tests/test_x.py:2\nN0 = 'x")
    assert "# tests/test_x.py:81\nN79 = 'x" in item.context
