"""`renovate.json5` read as text, the way the suites that hold its rules read it.

There is no JSON5 parser here, so a suite that holds a renovate rule finds
the rule's entry in the file's text and reads its keys one line at a time.
The shape that makes that sound is the file's own: no `packageRules` entry
nests an object, and each key a rule sets sits on a line of its own. A brace
in a comment or inside a string -- a quantifier in a rule's regular
expression, `[0-9a-f]{12}` -- is passed over, so the braces a scan finds are
the ones that delimit an entry.

A custom manager's `matchStrings` are regular expressions, which a suite
holds by writing each one out in Python and checking it two ways: that
`renovate.json5` holds exactly that pattern, and that the pattern matches
the file it is meant to read. `as_renovate_spells_it` and
`as_python_spells_it` are the two spellings those checks need.
"""

from __future__ import annotations

import re

#: A string, in either quote: `renovate.json5` writes a value in single quotes,
#: and in double quotes where the value holds an apostrophe.
_STRING = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")


def _structure(text: str) -> str:
    """`text` with each comment line and the inside of each string blanked, every offset kept."""

    def blank(line: str) -> str:
        if line.lstrip().startswith('//'):
            return ' ' * len(line)
        return _STRING.sub(lambda found: found[0][0] + ' ' * (len(found[0]) - 2) + found[0][-1], line)

    return '\n'.join(blank(line) for line in text.split('\n'))


def _uncommented(text: str) -> str:
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('//'))


def package_rule(config: str, at: int) -> tuple[int, str]:
    """The `packageRules` entry around offset `at`: where it starts, and its lines bar comments.

    An entry is the braces around the offset, which holds because no entry
    nests an object.
    """
    structure = _structure(config)
    start = structure.rindex('{', 0, at)
    return start, _uncommented(config[start : structure.index('}', at)])


def package_rules(config: str) -> list[str]:
    """Each `packageRules` entry of `renovate.json5`, with its comment lines dropped.

    An entry is a brace-delimited object, and none nests an object, which is
    what lets a text scan find them.
    """
    start = config.index('packageRules: [')
    block = config[start : config.index('customManagers: [', start)]
    return [_uncommented(block[found.start() : found.end()]) for found in re.finditer(r'\{[^{}]*\}', _structure(block))]


def listed(rule: str, key: str) -> list[str]:
    """The strings a rule lists under `key`, or none when it has no such key."""
    found = re.search(rf'^\s*{key}: \[([^\]]*)\],$', rule, re.MULTILINE)
    return re.findall(r"'([^']*)'", found[1]) if found else []


def scalar(rule: str, key: str) -> str | None:
    """The string a rule sets `key` to, or `None` when it does not set it."""
    found = re.search(rf"^\s*{key}: '([^']*)',$", rule, re.MULTILINE)
    return found[1] if found else None


def group(rule: str) -> list[str]:
    """The group name and slug a rule sets, in the order it writes them."""
    return re.findall(r"^\s*group(?:Name|Slug): '([^']*)',$", rule, re.MULTILINE)


def constraints(config: str) -> dict[str, str]:
    """The tool versions the top-level `constraints` block names, by tool.

    The block is the one object at two spaces' indent opened by
    `constraints: {`, and each entry sits on a line of its own.
    """
    blocks = re.findall(r'^  constraints: \{\n(.*?)^  \},$', config, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, f'renovate.json5 holds {len(blocks)} top-level constraints blocks'
    return dict(re.findall(r"^    (\w+): '([^']*)',$", blocks[0], re.MULTILINE))


def as_renovate_spells_it(pattern: str) -> str:
    """`pattern` as `renovate.json5` holds it: a JSON5 single-quoted string, each backslash doubled."""
    return "'" + pattern.replace('\\', '\\\\') + "'"


def as_python_spells_it(pattern: str) -> re.Pattern[str]:
    """`pattern` compiled as Python spells it: a named group is `(?P<...>`, renovate's regex engine writes `(?<...>`."""
    return re.compile(pattern.replace('(?<', '(?P<'))
