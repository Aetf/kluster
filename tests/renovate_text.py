"""`renovate.json5` read as text, the way the suites that hold its rules read it.

There is no JSON5 parser here, so a suite that holds a renovate rule finds
the rule's entry in the file's text and reads its keys one line at a time.
The shape that makes that sound is the file's own: no `packageRules` entry
nests an object, no comment inside one carries a brace, and each key a rule
sets sits on a line of its own.
"""

from __future__ import annotations

import re


def package_rule(config: str, at: int) -> tuple[int, str]:
    """The `packageRules` entry around offset `at`: where it starts, and its lines bar comments.

    An entry is the braces around the offset, which holds because no entry
    nests an object and no comment inside one carries a brace.
    """
    start = config.rindex('{', 0, at)
    lines = config[start : config.index('}', at)].splitlines()
    return start, '\n'.join(line for line in lines if not line.lstrip().startswith('//'))


def package_rules(config: str) -> list[str]:
    """Each `packageRules` entry of `renovate.json5`, with its comment lines dropped.

    An entry is a brace-delimited object; none nests an object and no comment
    inside one carries a brace, which is what lets a text scan find them.
    """
    start = config.index('packageRules: [')
    block = config[start : config.index('customManagers: [', start)]
    lines = (line for line in block.splitlines() if not line.lstrip().startswith('//'))
    return re.findall(r'\{[^{}]*\}', '\n'.join(lines))


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
