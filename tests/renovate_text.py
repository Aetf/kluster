"""`renovate.json5` read as text, the way the suites that hold its rules read it.

There is no JSON5 parser here, so a suite that holds a renovate rule finds
the rule's entry in the file's text, finds the keys the rule sets, and reads
the value of each from the line its key starts. The shape that makes that
sound is the file's own: no `packageRules` entry nests an object, each key a
rule sets sits on a line of its own, and a line feed is the one line break.
Text outside that shape fails rather than reads: an entry that nests an
object and a line break of another kind fail an assertion, and a key read
off a line it does not start is `Unread`. The scan reads strings and
comments, of both kinds, as JSON5 does, so a brace in a comment or inside a
string -- a quantifier in a rule's regular expression, `[0-9a-f]{12}` -- is
passed over, and the braces a scan finds are the ones that delimit an
entry; a key inside a comment is no key.

A read fails closed. When a rule sets a key and the scan cannot read its
value, `listed` and `scalar` raise `Unread` rather than answer as if the
rule did not set it, so a check that skips a rule setting no group never
skips one whose group it could not read; `reaches` alone catches it, and
takes the matcher as matching.

A rule that sets a key twice is read at the last value, which is the value
renovate reads: it parses a `.json5` file with `JSON5.parse`, which assigns
each key in turn and does not refuse a duplicate. Taking such a rule as
unreadable would fail closed too, through `Unread`; reading the last value
is the more precise of the two.

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

Whether renovate keeps an update at all is `enabled`, which reads the
matching rules and the top-level object named for the update's type
(`top_level_object`) the way renovate merges them, each through the same
key reading, so a top-level object the scan cannot read fails rather than
reads as setting nothing.
"""

from __future__ import annotations

import fnmatch
import re

#: A string, in either quote: `renovate.json5` writes a value in single quotes,
#: and in double quotes where the value holds an apostrophe.
_STRING = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")

#: A comment, as JSON5 reads one: `//` to the end of its line, or `/* … */`.
_COMMENT = r'//[^\n]*|/\*[\s\S]*?\*/'

#: What JSON5 reads as one piece wherever it starts, scanned from the left so
#: that whichever opens first takes the text: a string, which an escaped
#: line break continues onto the next line, or a comment. A `//` or `/*`
#: inside a string is part of it, and a quote inside a comment is part of that.
_TOKEN = re.compile(rf"""'(?:[^'\\\n]|\\[\s\S])*'|"(?:[^"\\\n]|\\[\s\S])*"|{_COMMENT}""")


#: The line breaks JSON5 reads besides a line feed, each of which ends a `//`
#: comment: the scan reads none of them, so a text holding one is refused.
_OTHER_BREAK = re.compile('[\r\u2028\u2029]')


def _lf_only(text: str) -> str:
    """`text`, once it is shown to break its lines with a line feed alone."""
    assert not _OTHER_BREAK.search(text), 'the scan reads line feeds alone as line breaks'
    return text


def _blank(text: str) -> str:
    """`text` with every character but a line break turned into a space."""
    return re.sub(r'[^\n]', ' ', text)


def _structure(text: str) -> str:
    """`text` with each comment and the inside of each string blanked, every offset kept."""
    return _TOKEN.sub(
        lambda found: _blank(found[0]) if found[0][0] == '/' else found[0][0] + _blank(found[0][1:-1]) + found[0][-1],
        _lf_only(text),
    )


def _uncommented(text: str) -> str:
    """`text` with its comments passed over, as JSON5 reads them: as whitespace.

    A `/* … */` comment is blanked, its line breaks kept, and a `//` comment
    on a line of its own is dropped. A `//` comment after other text on its
    line stays: `listed` passes over one among a list's entries, and a key's
    value with one after it on its closing line is a value no read can read.
    """

    def uncomment(found: re.Match[str]) -> str:
        token = found[0]
        if token.startswith('/*'):
            return _blank(token)
        if token.startswith('//') and not text[text.rfind('\n', 0, found.start()) + 1 : found.start()].strip():
            return ''
        return token

    return _TOKEN.sub(uncomment, _lf_only(text))


#: A string or a comment, or else a name, which may spell a character as a
#: `\u` escape.
_WORD = re.compile(rf'{_TOKEN.pattern}|(?:[\w$]|\\u[0-9A-Fa-f]{{4}})+')

#: Whitespace and comments, which JSON5 reads alike.
_GAP = rf'(?:\s|{_COMMENT})*'

#: What makes the string or name before it a key: a colon, past any
#: whitespace and comments.
_COLON = re.compile(rf'{_GAP}:')

#: A key's colon and the gap after it, up to where its value starts.
_TO_VALUE = re.compile(rf'{_GAP}:{_GAP}')


def _keys(rule: str) -> list[tuple[str, int]]:
    """Each key `rule` sets, as the name JSON5 reads, with the offset its spelling starts at, in order.

    A string or a name is a key when a colon follows it, which in an entry
    that nests no object is the one place a colon goes.
    """
    keys: list[tuple[str, int]] = []
    for found in _WORD.finditer(_lf_only(rule)):
        word = found[0]
        if word.startswith('/') or not _COLON.match(rule, found.end()):
            continue
        keys.append((string_value(word[1:-1] if word[0] in '\'"' else word), found.start()))
    return keys


def matchers(rule: str) -> list[str]:
    """The keys a rule matches on, in the order it sets them: renovate tells them from the rest by their prefix."""
    return [name for name, _ in _keys(rule) if name.startswith(('match', 'exclude'))]


def _set_at(rule: str, key: str) -> int | None:
    """Where the value JSON5 keeps for `key` starts, past the last spelling of the key.

    `None` when the rule does not set the key, and when that spelling is
    not the plain name, first on its line and followed by `: `.
    """
    setting = [at for name, at in _keys(rule) if name == key]
    if not setting:
        return None
    at = setting[-1]
    written = re.compile(rf'[ \t]*{re.escape(key)}: ').match(rule, rule.rfind('\n', 0, at) + 1)
    return written.end() if written is not None and written.end() == at + len(key) + 2 else None


def package_rule(config: str, at: int) -> tuple[int, str]:
    """The `packageRules` entry around offset `at`: where it starts, and its text, its comments passed over.

    An entry is the braces around the offset, which holds because no entry
    nests an object.
    """
    structure = _structure(config)
    start = structure.rindex('{', 0, at)
    return start, _uncommented(config[start : structure.index('}', at)])


def package_rules(config: str) -> list[str]:
    """Each `packageRules` entry of `renovate.json5`, its comments passed over.

    An entry is a brace-delimited object inside the brackets of
    `packageRules: [`, and none nests an object, which is what lets a text
    scan find them. A rule may set an object -- renovate takes
    `prBodyDefinitions` and `constraints` there -- and an entry that nests
    one fails here rather than read as a rule it is not.
    """
    structure = _structure(config)
    start = structure.index('packageRules: [') + len('packageRules: [')
    end = _closing(structure, start)
    entries = list(re.compile(r'\{[^{}]*\}').finditer(structure, start, end))
    assert structure.count('{', start, end) == len(entries), (
        'a packageRules entry nests an object, which tests/renovate_text.py does not read'
    )
    return [_uncommented(config[found.start() : found.end()]) for found in entries]


def _closing(structure: str, start: int) -> int:
    """Where the `]` that closes the list opened just before `start` sits."""
    depth = 1
    for found in re.compile(r'[\[\]]').finditer(structure, start):
        depth += 1 if found[0] == '[' else -1
        if not depth:
            return found.start()
    raise AssertionError('packageRules is never closed')


class Unread(AssertionError):
    """A rule sets the key, and the scan cannot read the value JSON5 keeps."""


def _refuse_unread(rule: str, key: str) -> None:
    """Raise `Unread` when `rule` sets `key`: called once the scan has found no value it can read."""
    if any(name == key for name, _ in _keys(rule)):
        raise Unread(f'{key} is set in a spelling this scan does not read:\n{rule}')


#: What a list holds between its brackets besides commas and whitespace, once
#: its `/* … */` comments are blanked: a string, or a `//` comment, which takes
#: the rest of its line and gives none of it back, so a `],` it ends with
#: never closes the list.
_LISTED = re.compile(rf'{_STRING.pattern}|//[^\n]*+')

#: A list read whole, from its opening bracket to a `],` that ends its line.
_LIST = re.compile(rf'\[((?:[\s,]|{_LISTED.pattern})*)\],$', re.MULTILINE)


def listed(rule: str, key: str) -> list[str]:
    """The strings a rule lists under `key`, each as the file writes it between its quotes.

    An entry is read in either quote, so a list mixing the two is read whole,
    and a `]` inside an entry is part of it; a comment among the entries is
    passed over. A rule that sets the key twice is read at the last list.
    The list is empty when the rule does not set the key. The scan reads a
    list when the last spelling of its key is the plain name, first on its
    line and followed by `: `, and the list holds strings each on one line
    and closes with `],` at the end of a line; any other is `Unread`.
    """
    rule = _uncommented(rule)
    at = _set_at(rule, key)
    found = None if at is None else _LIST.match(rule, at)
    if found is None:
        _refuse_unread(rule, key)
        return []
    return [entry[1:-1] for entry in _LISTED.findall(found[1]) if not entry.startswith('/')]


#: A string a key is set to, followed by a comma and the end of its line.
_SCALAR = re.compile(rf'({_STRING.pattern}),$', re.MULTILINE)


def scalar(rule: str, key: str) -> str | None:
    """The string a rule sets `key` to, as the file writes it between its quotes; `None` when it does not set it.

    A rule that sets the key twice is read at the last value. The scan reads
    a value when the last spelling of its key is the plain name, first on
    its line and followed by `: `, and the value is a string on that line
    followed by a comma and the end of the line; any other is `Unread`.
    """
    rule = _uncommented(rule)
    at = _set_at(rule, key)
    found = None if at is None else _SCALAR.match(rule, at)
    if found is None:
        _refuse_unread(rule, key)
        return None
    return found[1][1:-1]


#: A boolean a key is set to, followed by a comma and the end of its line.
_FLAG = re.compile(r'(true|false),$', re.MULTILINE)


def flag(rule: str, key: str) -> bool | None:
    """The boolean a rule sets `key` to; `None` when it does not set it.

    Read as `scalar` reads a string: at the last value, when the last
    spelling of the key is the plain name, first on its line and followed by
    `: `, and the value is `true` or `false` followed by a comma and the end
    of the line; any other is `Unread`.
    """
    rule = _uncommented(rule)
    at = _set_at(rule, key)
    found = None if at is None else _FLAG.match(rule, at)
    if found is None:
        _refuse_unread(rule, key)
        return None
    return found[1] == 'true'


def group(rule: str) -> list[str]:
    """The group name and slug a rule sets, each as `scalar` reads it, in the order it first writes them.

    A group the scan cannot read is `Unread`, as it is for `scalar`.
    """
    named = dict.fromkeys(name for name, _ in _keys(rule) if name in {'groupName', 'groupSlug'})
    return [value for name in named if (value := scalar(rule, name)) is not None]


def constraints(config: str) -> dict[str, str]:
    """The tool versions the top-level `constraints` block names, by tool.

    The block is the one object at two spaces' indent opened by
    `constraints: {`, and each entry sits on a line of its own.
    """
    blocks = re.findall(r'^  constraints: \{\n(.*?)^  \},$', config, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 1, f'renovate.json5 holds {len(blocks)} top-level constraints blocks'
    return dict(re.findall(r"^    (\w+): '([^']*)',$", blocks[0], re.MULTILINE))


def top_level_object(config: str, key: str) -> str | None:
    """The object `renovate.json5` sets its top-level `key` to, its comments passed over; `None` when it sets none.

    A top-level key is one set in the file's outermost object, in any
    spelling JSON5 reads as the name, and a key set twice is read at its last
    value. The object is read when it nests no object, which is the shape the
    keys of a rule are read in (`scalar`, `flag`); a value that is no object,
    or an object nesting one, is `Unread`.
    """
    structure = _structure(config)
    setting = [
        at
        for name, at in _keys(config)
        if name == key and structure.count('{', 0, at) - structure.count('}', 0, at) == 1
    ]
    if not setting:
        return None
    word = _WORD.match(config, setting[-1])
    assert word is not None
    to_value = _TO_VALUE.match(config, word.end())
    assert to_value is not None
    at = to_value.end()
    if structure[at : at + 1] != '{':
        raise Unread(f'the top-level {key} is no object')
    end = structure.index('}', at)
    if '{' in structure[at + 1 : end]:
        raise Unread(f'the top-level {key} nests an object, which this scan does not read')
    return _uncommented(config[at : end + 1])


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


def string_value(written: str) -> str:
    """The value of a JSON5 string the file writes as `written` between its quotes: each escape undone.

    The one decoder of a string `renovate.json5` writes: a key's name, a
    match pattern, and a custom manager's template and match strings.
    """
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


def _settled(pattern: str, value: str) -> bool | None:
    """Whether one renovate match pattern matches `value`, or `None` where this reading cannot settle it.

    Unsettled are what `matches` names, and a glob's match through a
    wildcard or a class, which `fnmatch` makes across a `/` where
    `minimatch` does not; a glob's miss is settled.
    """
    pattern = string_value(pattern)
    if pattern.startswith('!'):
        return None
    regex = _regex(pattern)
    if regex is not None:
        source, flags = regex
        if _UNSETTLED_REGEX.search(source):
            return None
        try:
            return re.search(source, value, flags) is not None
        except re.error:
            return None
    if any(char in pattern for char in '{(\\') or '[:' in pattern:
        return None
    if pattern == '*':
        # Renovate matches a lone `*` against everything, before `minimatch`.
        return True
    # `fnmatch` spells a negated class `[!…]` alone; `minimatch` takes `[^…]`
    # too. A `**/` segment spans zero or more directories, which `fnmatch`'s
    # `*`, crossing `/`, covers. That crossing makes `fnmatch` match more than
    # `minimatch`, whose `*`, `?` and classes stop at a `/`, so a miss is
    # settled and a match through any of them is not.
    if not fnmatch.fnmatchcase(value.lower(), pattern.lower().replace('[^', '[!').replace('**/', '*')):
        return False
    return None if any(char in pattern for char in '*?[') else True


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
    return _settled(pattern, value) is not False


def reaches(rule: str, presents: dict[str, tuple[str, ...]]) -> bool:
    """Whether a rule can match an update presenting these values to its matchers.

    A `match…` key clears the rule when no pattern it lists matches any value
    the update presents for it. A key the update presents no value for, and
    a key whose list this scan cannot read (`Unread`), are taken as
    matching, so a rule is only cleared by a matcher that provably misses.
    An `exclude…` key never clears it: an exclusion takes effect on a match,
    and `matches` can prove only a miss. A rule setting a deprecated key
    renovate merges into another matcher's list before matching --
    `match…Patterns`, `match…Prefixes` -- is taken as matching.
    """
    keys = dict.fromkeys(name for name, _ in _keys(rule))
    if any(re.fullmatch(r'match\w+(?:Patterns|Prefixes)', key) for key in keys):
        return True
    for key in filter(re.compile(r'match\w+').fullmatch, keys):
        values = presents.get(key)
        try:
            patterns = listed(rule, key)
        except Unread:
            continue
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
        if any(name == key for name, _ in _keys(rule)) and reaches(rule, presents)
    ]
    return setting[-1] if setting else None


def provably_matches(rule: str, presents: dict[str, tuple[str, ...]]) -> bool:
    """Whether a rule matches an update presenting these values, as far as this reading can prove it.

    Every `match…` key the rule sets has a value presented for it, its list
    is read, holds no negated pattern, and has a pattern this reading settles
    as matching one of those values (`matches`); and the rule sets no
    `exclude…` key, nor a deprecated key renovate merges into another
    matcher's list (`reaches`). A rule this cannot prove may still match.
    Each value presented is one the update holds once renovate has merged
    its rules, not one it might hold, as `reaches` also allows: a value
    presented in excess would prove a match on it.
    """
    keys = dict.fromkeys(name for name, _ in _keys(rule))
    if any(key.startswith('exclude') or re.fullmatch(r'match\w+(?:Patterns|Prefixes)', key) for key in keys):
        return False
    for key in filter(re.compile(r'match\w+').fullmatch, keys):
        values = presents.get(key)
        try:
            patterns = listed(rule, key)
        except Unread:
            return False
        if values is None or not patterns or any(string_value(pattern).startswith('!') for pattern in patterns):
            return False
        if not any(_settled(pattern, value) for pattern in patterns for value in values):
            return False
    return True


def enabled(config: str, presents: dict[str, tuple[str, ...]]) -> bool:
    """Whether renovate keeps an update enabled once it merges its package rules over its update type's object.

    Renovate merges the package rules, then the top-level object named for
    the update's type -- `patch: { enabled: false }` -- and then the package
    rules again, and drops an update whose `enabled` ends up `false`
    (renovate 44.249.1, `lib/workers/repository/updates/flatten.ts`). So
    the last matching rule that sets `enabled` decides, wherever it sits
    among the rules; where none sets it, the object for the update's type
    does, and where that sets nothing, the update is enabled. `presents`
    names the update's one type under `matchUpdateTypes`.

    The reading fails closed. The rules that set `enabled` are walked from
    the last, passing over each that provably misses the update (`reaches`);
    the first one left decides only when it provably matches
    (`provably_matches`), and is otherwise `Unread`, since a rule this
    reading cannot settle might not apply. Each value is read by `flag`, so
    a setting it cannot read is `Unread` too.

    Only that merge is read. What else drops or keeps an update is not:
    `ignoreDeps`, `ignorePaths`, `enabledManagers`, an `enabled` set at the
    top level or on a manager, and whatever a preset in `extends` sets.
    """
    (update_type,) = presents['matchUpdateTypes']
    setting_rules = [rule for rule in package_rules(config) if any(name == 'enabled' for name, _ in _keys(rule))]
    for rule in reversed(setting_rules):
        if not reaches(rule, presents):
            continue
        if not provably_matches(rule, presents):
            raise Unread(f'whether this rule applies to the update is not read here:\n{rule}')
        setting = flag(rule, 'enabled')
        assert setting is not None
        return setting
    block = top_level_object(config, update_type)
    setting = None if block is None else flag(block, 'enabled')
    return setting is not False


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
    pattern = string_value(written)
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
