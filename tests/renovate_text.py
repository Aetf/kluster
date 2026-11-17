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

A suite that holds which rules an update reaches reads each rule's match
patterns the way renovate does: `matches` for one pattern, `reaches` for a
rule and `effective` for the rule a key is taken from. The reading fails
closed. A pattern whose answer it cannot settle is taken as matching, so a
rule is cleared only by a matcher that provably misses, and a reading that
says no rule after another can reach an update holds in renovate too.
`tests/test_renovate_text.py` holds `matches` to renovate's own rules.
"""

from __future__ import annotations

import fnmatch
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


#: What a list holds between its brackets besides commas and whitespace: a
#: string, a `//` comment, which takes the rest of its line and gives none of
#: it back, so a `],` it ends with never closes the list, or a `/* … */` comment.
_LISTED = re.compile(rf'{_STRING.pattern}|//[^\n]*+|/\*(?:[^*]|\*(?!/))*\*/')


def listed(rule: str, key: str) -> list[str]:
    """The strings a rule lists under `key`, each as the file writes it between its quotes.

    An entry is read in either quote, so a list mixing the two is read whole,
    and a `]` inside an entry is part of it; a comment among the entries is
    passed over. The list is empty without the key, and when the scan cannot
    read it whole: an entry that is no string on one line -- a number, a
    name, a string continued onto the next line -- or a closing `]` not
    followed by a comma and the end of its line.
    """
    found = re.search(rf'^\s*{key}: \[((?:[\s,]|{_LISTED.pattern})*)\],$', rule, re.MULTILINE)
    return [entry[1:-1] for entry in _LISTED.findall(found[1]) if not entry.startswith('/')] if found else []


def scalar(rule: str, key: str) -> str | None:
    """The string a rule sets `key` to, as the file writes it between its quotes; `None` when it does not set it."""
    found = re.search(rf'^\s*{key}: ({_STRING.pattern}),$', rule, re.MULTILINE)
    return found[1][1:-1] if found else None


def group(rule: str) -> list[str]:
    """The group name and slug a rule sets, in the order it writes them."""
    return [found[1:-1] for found in re.findall(rf'^\s*group(?:Name|Slug): ({_STRING.pattern}),$', rule, re.MULTILINE)]


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


#: A JSON5 string's escapes: a two-digit `\x`, a four-digit `\u`, or any
#: one character after the backslash, a line break included.
_ESCAPE = re.compile(r'\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|.)', re.DOTALL)

#: What a one-character escape stands for, where it is not the character
#: itself; an escaped line break continues the string.
_ESCAPED = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '0': '\0', '\n': ''}


def _value(written: str) -> str:
    """The value of a JSON5 string the file writes as `written` between its quotes: each escape undone."""
    return _ESCAPE.sub(
        lambda escape: chr(int(escape[1][1:], 16)) if len(escape[1]) > 1 else _ESCAPED.get(escape[1], escape[1]),
        written,
    )


def _regex(pattern: str) -> tuple[str, re.RegexFlag] | None:
    """The source and flags of the regular expression renovate reads `pattern` as, or `None` for a glob.

    Renovate's `isRegexMatch` and `parseRegexMatch`: a pattern is one when it
    opens with `/` or `!/` and closes with `/` or `/i`, which a lone `/`
    does too, as the empty expression; a final `i` ignores case.
    """
    if not (re.match(r'!?/', pattern) and re.search(r'/i?$', pattern)):
        return None
    source = re.sub(r'/i?$', '', re.sub(r'^!?/', '', pattern))
    return source, re.IGNORECASE if pattern.endswith('i') else re.NOFLAG


#: Regular-expression syntax renovate's engine and Python's do not read
#: alike: lookaround and backreferences, which RE2 refuses (renovate then
#: reads the pattern as a glob), and a POSIX class, which RE2 reads as a
#: class and Python as a set followed by a literal `]`.
_UNSETTLED_REGEX = re.compile(r'\(\?<?[=!]|\\[1-9]|\[:')


def matches(pattern: str, value: str) -> bool:
    """Whether one renovate match pattern -- a `/regex/` or `/regex/i`, a glob or a literal -- might match `value`.

    `pattern` is what `listed` returns, the file's text between the quotes,
    and is read as renovate reads it: as the JSON5 string's value. A regex is
    searched for, and a glob is matched ignoring case, as renovate's
    `minimatch` call is. What this reading cannot settle is taken as
    matching: a negated pattern (`!…`), whose effect depends on the rest of
    its list; a regex Python cannot compile, or one the two engines read
    apart; and glob
    syntax `fnmatch` does not share with `minimatch` -- braces, extglobs,
    escapes and POSIX classes.
    """
    pattern = _value(pattern)
    if pattern.startswith('!'):
        return True
    regex = _regex(pattern)
    if regex is not None:
        source, flags = regex
        if _UNSETTLED_REGEX.search(source):
            return True
        try:
            return re.search(source, value, flags) is not None
        except re.error:
            return True
    if any(char in pattern for char in '{(\\') or '[:' in pattern:
        return True
    # `fnmatch` spells a negated class `[!…]` alone; `minimatch` takes `[^…]`
    # too. A `**/` segment spans zero or more directories, which `fnmatch`'s
    # `*`, crossing `/`, covers.
    return fnmatch.fnmatchcase(value.lower(), pattern.lower().replace('[^', '[!').replace('**/', '*'))


def reaches(rule: str, presents: dict[str, tuple[str, ...]]) -> bool:
    """Whether a rule can match an update presenting these values to its matchers.

    A `match…` key clears the rule when no pattern it lists matches any value
    the update presents for it. A key the update presents no value for, and
    a key whose list this scan cannot read, are taken as matching, so a rule
    is only cleared by a matcher that provably misses. An `exclude…` key
    never clears it: an exclusion takes effect on a match, and `matches` can
    prove only a miss. A rule setting a deprecated key renovate merges into
    another matcher's list before matching -- `match…Patterns`,
    `match…Prefixes` -- is taken as matching.
    """
    if re.search(r'^\s*match\w+(?:Patterns|Prefixes):', rule, re.MULTILINE):
        return True
    for key in re.findall(r'^\s*(match\w+):', rule, re.MULTILINE):
        values = presents.get(key)
        patterns = listed(rule, key)
        if values is None or not patterns:
            continue
        if not any(matches(pattern, value) for pattern in patterns for value in values):
            return False
    return True


def effective(config: str, presents: dict[str, tuple[str, ...]], key: str) -> str | None:
    """The rule renovate takes `key` from for an update: the last one that sets it and can match.

    Every matching rule is merged in order, so a later one overrides an
    earlier one on any key both set. `None` when no rule sets the key.
    """
    setting = [
        rule
        for rule in package_rules(config)
        if re.search(rf'^\s*{key}:', rule, re.MULTILINE) and reaches(rule, presents)
    ]
    return setting[-1] if setting else None


def allowed_versions(rule: str) -> re.Pattern[str] | None:
    """The `allowedVersions` a rule sets, as the regular expression renovate reads; `None` when it sets none.

    Renovate reads the setting as a regex when it is spelled as one and RE2
    compiles it, and otherwise as a range in the dependency's versioning,
    which this reading does not hold: a setting that is no regex fails here,
    as does a negated one, one with a template in it, which renovate expands
    first, and one the two engines read apart.
    """
    written = scalar(rule, 'allowedVersions')
    if written is None:
        return None
    pattern = _value(written)
    regex = _regex(pattern)
    assert regex is not None, f'allowedVersions {written} is not a pattern'
    assert not pattern.startswith('!'), f'allowedVersions {written} is negated'
    assert '{{' not in pattern, f'allowedVersions {written} is a template'
    source, flags = regex
    assert not _UNSETTLED_REGEX.search(source), f'allowedVersions {written} is no regex this reading holds'
    return re.compile(source, flags)


def next_line(version: str) -> str:
    """The first release of the line after `version`'s: its next minor, with or without a leading `v`."""
    major, minor = version.removeprefix('v').split('.')[:2]
    return f'{major}.{int(minor) + 1}.0'
