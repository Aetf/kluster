"""`renovate_text` reads a rule's match patterns the way renovate does, or fails closed.

Renovate's reading of a pattern is `lib/util/string-match.ts`, cited here at
renovate 44.249.1
(https://github.com/renovatebot/renovate/blob/44.249.1/lib/util/string-match.ts):

-   `isRegexMatch` (lines 73-80) takes a pattern as a regex when it opens
    with `/` or `!/` and closes with `/` or `/i`; `parseRegexMatch` (82-92)
    strips both ends and ignores case when the pattern ends in `i`, and
    `getRegexPredicate` (94-106) searches for it. A regex the engine refuses
    to compile falls through to the glob.
-   Anything else is a glob, matched by `minimatch` with `dot` and `nocase`
    (line 17), and a lone `*` matches everything (line 22).
-   A leading `!` negates the pattern, and a list with negated patterns
    matches only where every one of them does (lines 29-60).

The suites hold `renovate.json5` with the reading, and clear a rule when it
says the rule misses, so the reading may report a match renovate would not
make but never miss one renovate makes. The cases below are spelled as the
file writes them between the quotes, each backslash doubled, since that is
what `listed` hands `matches`.
"""

from __future__ import annotations

import pytest
from renovate_text import (
    Unread,
    allowed_versions,
    effective,
    enabled,
    flag,
    group,
    listed,
    matches,
    next_line,
    package_rules,
    provably_matches,
    reaches,
    scalar,
    string_value,
    top_level_object,
)

#: Patterns renovate matches against the value, each with the rule that makes
#: it a match.
RENOVATE_MATCHES = [
    pytest.param(r'/^Pulumi\\.yaml$/', 'Pulumi.yaml', id='a-regex-is-the-strings-value'),
    pytest.param('/^talosctl$/i', 'TalosCtl', id='a-final-i-ignores-case'),
    pytest.param('/talos/', 'siderolabs/talos-linux', id='a-regex-is-searched-for'),
    pytest.param('/', 'talosctl', id='a-lone-slash-is-the-empty-regex'),
    pytest.param('Talosctl', 'talosctl', id='a-literal-ignores-case'),
    pytest.param('SideroLabs/*', 'siderolabs/talos', id='a-glob-ignores-case'),
    pytest.param('*', 'docker/talos', id='a-lone-star-matches-everything'),
    pytest.param('docker/*', 'docker/.hidden', id='a-glob-matches-a-dotfile'),
    pytest.param('**/Pulumi.yaml', 'Pulumi.yaml', id='a-globstar-spans-no-directory'),
    pytest.param('{talosctl,kubectl}', 'talosctl', id='braces-expand'),
    pytest.param('@(talosctl|kubectl)', 'kubectl', id='an-extglob'),
    pytest.param(r'talos\\*', 'talos*', id='an-escaped-glob-character-is-literal'),
    pytest.param('[[:alpha:]]alosctl', 'talosctl', id='a-posix-class'),
    pytest.param('[^k]alosctl', 'talosctl', id='a-caret-negates-a-class'),
]

#: Patterns renovate does not match against the value, and that the reading
#: settles as misses: without them a reading that matched everything would pass.
RENOVATE_MISSES = [
    pytest.param(r'/^Pulumi\\.yaml$/', 'PulumiXyaml', id='an-escaped-dot-is-a-dot'),
    pytest.param('/^talosctl$/', 'TalosCtl', id='a-regex-without-i-keeps-case'),
    pytest.param('/^talos$/', 'talosctl', id='a-regex-anchors'),
    pytest.param('talosctl', 'kubectl', id='a-literal'),
    pytest.param('siderolabs/*', 'docker/talos', id='a-glob'),
    pytest.param('[^t]alosctl', 'talosctl', id='a-caret-class-excludes'),
]

#: Patterns whose answer the reading cannot settle, which it takes as matching
#: whatever renovate's answer: a negated pattern's depends on its list, a
#: regex Python cannot compile is not one Python can search for, one RE2
#: refuses -- lookaround, a backreference -- renovate reads as a glob instead,
#: and a POSIX class is a class to RE2 and a set and a `]` to Python.
UNSETTLED = [
    pytest.param('!talosctl', 'talosctl', id='a-negated-glob'),
    pytest.param('!/^talosctl$/', 'kubectl', id='a-negated-regex'),
    pytest.param('/^(?!kube)talosctl$/', 'kubectl', id='lookaround-re2-refuses'),
    pytest.param(r'/^(t)\\1$/', 'tx', id='a-backreference-re2-refuses'),
    pytest.param('/^(talos$/', 'talosctl', id='a-regex-nothing-compiles'),
    pytest.param('/^(?<tool>talos)ctl$/', 'talosctl', id='a-named-group-python-does-not-compile'),
    pytest.param('/^[[:alpha:]]+$/', 'talosctl', id='a-posix-class-in-a-regex'),
]


@pytest.mark.parametrize(('pattern', 'value'), RENOVATE_MATCHES)
def test_what_renovate_matches_is_matched(pattern: str, value: str) -> None:
    assert matches(pattern, value)


@pytest.mark.parametrize(('pattern', 'value'), RENOVATE_MISSES)
def test_what_renovate_misses_and_the_reading_settles_is_missed(pattern: str, value: str) -> None:
    assert not matches(pattern, value)


@pytest.mark.parametrize(('pattern', 'value'), UNSETTLED)
def test_what_the_reading_cannot_settle_is_taken_as_matching(pattern: str, value: str) -> None:
    assert matches(pattern, value)


def test_a_list_is_read_in_either_quote() -> None:
    """`renovate.json5` writes a string in double quotes where it holds an apostrophe."""
    rule = """  {
    matchDepNames: ['kubectl', "talosctl", 'cmctl', "it's", 'it\\'s'],
  }"""
    assert listed(rule, 'matchDepNames') == ['kubectl', 'talosctl', 'cmctl', "it's", "it\\'s"]


def test_a_setting_and_a_group_are_read_in_either_quote() -> None:
    rule = """  {
    groupName: "the appliance's",
    groupSlug: 'appliance',
  }"""
    assert group(rule) == ["the appliance's", 'appliance']
    assert scalar(rule, 'groupName') == "the appliance's"


def test_a_rule_listing_the_update_in_mixed_quotes_is_not_cleared() -> None:
    rule = """  {
    matchDepNames: ['kubectl', "talosctl"],
    groupName: 'tools',
  }"""
    assert reaches(rule, {'matchDepNames': ('talosctl',)})
    assert not reaches(rule, {'matchDepNames': ('cmctl',)})


@pytest.mark.parametrize(
    'listing',
    [
        r"['/^TALOSCTL$/i']",
        r"['/^talos\\.?ctl$/']",
        "['TalosCtl']",
        "['{talosctl,kubectl}']",
        '["/^TALOSCTL$/i"]',
    ],
    ids=['regex-with-i', 'escaped-regex', 'glob-in-another-case', 'brace-glob', 'double-quoted-regex-with-i'],
)
def test_a_rule_reaching_the_update_by_renovates_reading_is_not_cleared(listing: str) -> None:
    rule = f"""  {{
    matchDepNames: {listing},
    groupName: 'tools',
  }}"""
    assert reaches(rule, {'matchDepNames': ('talosctl',)})


def test_an_entry_holding_a_bracket_is_read() -> None:
    """A character class holds a `]`, and the list around it is read whole, so a rule it misses is cleared."""
    rule = """  {
    matchFileNames: ['/^docker/[^/]+$/', "Pulumi.[jy]aml"],
  }"""
    assert listed(rule, 'matchFileNames') == ['/^docker/[^/]+$/', 'Pulumi.[jy]aml']
    assert reaches(rule, {'matchFileNames': ('docker/talos',)})
    assert not reaches(rule, {'matchFileNames': ('mise.toml',)})


def test_a_comment_among_the_entries_is_no_entry() -> None:
    rule = """  {
    matchDepNames: [
      'kubectl', // not 'talosctl', which has a rule of its own
      /* nor 'helm' */ 'cmctl',
    ],
  }"""
    assert listed(rule, 'matchDepNames') == ['kubectl', 'cmctl']


def test_a_bracket_ending_a_comment_does_not_close_the_list() -> None:
    """A `//` comment runs to the end of its line, so a list is read whole or not at all."""
    rule = """  {
    matchDepNames: [
      'kubectl', // not ['helm'],
      'talosctl',
    ], // a close the scan does not read
  }"""
    with pytest.raises(Unread):
        _ = listed(rule, 'matchDepNames')
    assert reaches(rule, {'matchDepNames': ('talosctl',)})


def test_a_block_comment_is_passed_over() -> None:
    """JSON5 reads a `/* … */` comment as whitespace: a brace in one delimits no entry, and a key in one is no key."""
    config = """  packageRules: [
    {
      matchDepNames: ['kubectl'],
      /* one group per tool { */
    },
    {
      /*
      matchDepNames: ['kubectl'],
      } */
      matchPackageNames: ['quay.io/cilium/**'],
      groupName: 'regrouped',
    },
  ],
  customManagers: [
  ],
"""
    first, second = package_rules(config)
    assert listed(first, 'matchDepNames') == ['kubectl']
    assert listed(second, 'matchDepNames') == []
    assert scalar(second, 'groupName') == 'regrouped'
    assert reaches(second, {'matchDepNames': ('cilium',), 'matchPackageNames': ('quay.io/cilium/cilium',)})


def test_a_key_set_twice_is_read_at_its_last_value() -> None:
    """JSON5 keeps the last value of a key a rule sets twice, so a read takes that one, or refuses one it cannot read."""
    rule = """  {
    matchDepNames: ['kubectl'],
    groupName: 'tools',
    matchDepNames: ['cilium'],
    groupName: 'regrouped',
  }"""
    assert listed(rule, 'matchDepNames') == ['cilium']
    assert scalar(rule, 'groupName') == 'regrouped'
    assert group(rule) == ['regrouped']
    assert reaches(rule, {'matchDepNames': ('cilium',)})
    quoted = """  {
    matchDepNames: ['kubectl'],
    'matchDepNames': ['cilium'],
  }"""
    with pytest.raises(Unread):
        _ = listed(quoted, 'matchDepNames')
    assert reaches(quoted, {'matchDepNames': ('cilium',)})


@pytest.mark.parametrize(
    'setting',
    ["groupName: 'regrouped', // rides with the tools", "groupName:'regrouped',"],
    ids=['a-comment-after-it', 'no-space-after-the-colon'],
)
def test_a_group_the_scan_cannot_read_is_refused(setting: str) -> None:
    """A check that skips a rule setting no group cannot skip one whose group the scan does not read."""
    rule = f"""  {{
    matchDepNames: ['cilium'],
    {setting}
  }}"""
    with pytest.raises(Unread):
        _ = scalar(rule, 'groupName')
    with pytest.raises(Unread):
        _ = group(rule)
    assert scalar(rule, 'groupSlug') is None


@pytest.mark.parametrize(
    'line_break', ['\r', '\u2028', '\u2029'], ids=['carriage-return', 'line-separator', 'paragraph-separator']
)
def test_a_line_break_the_scan_does_not_read_fails(line_break: str) -> None:
    """JSON5 ends a `//` comment at any line break, so a key after one is live; the scan refuses the text instead."""
    rule = f"""  {{
    matchDepNames: ['kubectl'],
    // tools{line_break}matchDepNames: ['cilium'],
  }}"""
    with pytest.raises(AssertionError, match='line feeds alone'):
        _ = reaches(rule, {'matchDepNames': ('cilium',)})
    with pytest.raises(AssertionError, match='line feeds alone'):
        _ = package_rules(f'  packageRules: [\n{rule},\n  ],\n')


def test_an_entry_nesting_an_object_fails() -> None:
    """A rule may set an object, which the scan does not read, so the list it sits in fails rather than drop it."""
    config = """  packageRules: [
    {
      matchDepNames: ['cilium'],
      groupName: 'regrouped',
      prBodyDefinitions: { Notes: 'see the changelog' },
    },
  ],
"""
    with pytest.raises(AssertionError, match='nests an object'):
        _ = package_rules(config)


def test_the_entries_are_the_ones_inside_the_list() -> None:
    """An object after the list's `]` is no entry, whatever key comes next."""
    config = """  packageRules: [
    {
      matchDepNames: ['cilium'],
    },
  ],
  lockFileMaintenance: {
    schedule: ['before 4am on monday'],
  },
  customManagers: [
  ],
"""
    (rule,) = package_rules(config)
    assert listed(rule, 'matchDepNames') == ['cilium']


def test_only_a_matcher_that_provably_misses_clears_a_rule() -> None:
    """A key the update presents no value for, an unreadable list and an exclusion all leave the rule standing."""
    rule = """  {
    matchDepNames: ['talosctl'],
    matchUpdateTypes: ['major'],
    excludeDepNames: ['kubectl'],
  }"""
    assert reaches(rule, {'matchDepNames': ('talosctl',)})
    assert reaches(rule, {'matchDepNames': ('talosctl',), 'excludeDepNames': ('talosctl',)})
    assert not reaches(rule, {'matchDepNames': ('kubectl',)})
    unreadable = """  {
    matchFileNames: ['docker/\\
Containerfile'],
  }"""
    with pytest.raises(Unread):
        _ = listed(unreadable, 'matchFileNames')
    assert reaches(unreadable, {'matchFileNames': ('Pulumi.yaml',)})


def test_a_rule_setting_a_key_renovate_merges_into_another_is_not_cleared() -> None:
    """Renovate appends `matchDepPatterns` to `matchDepNames` before matching, so the rule reaches `talosctl`."""
    rule = """  {
    matchDepNames: ['kubectl'],
    matchDepPatterns: ['^talos'],
  }"""
    assert reaches(rule, {'matchDepNames': ('talosctl',)})


def test_the_effective_rule_is_the_last_that_sets_the_key_and_can_match() -> None:
    config = """  packageRules: [
    {
      matchDepNames: ['talosctl'],
      schedule: ['* 0-3 1 * *'],
    },
    {
      matchDepNames: ["talosctl"],
      schedule: ['* 0-3 2 * *'],
    },
    {
      matchDepNames: ['kubectl'],
      schedule: ['* 0-3 3 * *'],
    },
  ],
  customManagers: [
  ],
"""
    found = effective(config, {'matchDepNames': ('talosctl',)}, 'schedule')
    assert found is not None
    assert listed(found, 'schedule') == ['* 0-3 2 * *']
    assert effective(config, {'matchDepNames': ('talosctl',)}, 'groupName') is None


def test_a_boolean_is_read_at_its_last_value() -> None:
    rule = """  {
    enabled: true,
    automerge: false,
    enabled: false,
  }"""
    assert flag(rule, 'enabled') is False
    assert flag(rule, 'automerge') is False
    assert flag(rule, 'ignoreTests') is None
    assert flag('  {\n    enabled: true,\n  }', 'enabled') is True


@pytest.mark.parametrize(
    'setting',
    ['enabled: false, // folded into the minors', 'enabled:false,', "enabled: 'false',"],
    ids=['a-comment-after-it', 'no-space-after-the-colon', 'a-string'],
)
def test_a_boolean_the_scan_cannot_read_is_refused(setting: str) -> None:
    with pytest.raises(Unread):
        _ = flag(f'  {{\n    {setting}\n  }}', 'enabled')


#: A configuration with the top-level `patch` object written with a key ahead
#: of `enabled` and comments around it, and a rule turning one dependency's
#: patches back on, ahead of the object in the text.
PATCHES_OFF = """{
  packageRules: [
    {
      matchDepNames: ['talosctl'],
      matchUpdateTypes: ['patch'],
      enabled: true,
    },
  ],
  patch: {
    // Folded into the minors.
    automerge: false,
    /* off */ enabled: false,
  },
}
"""


def test_a_top_level_object_is_read_whatever_comes_ahead_of_its_keys() -> None:
    block = top_level_object(PATCHES_OFF, 'patch')
    assert block is not None
    assert flag(block, 'enabled') is False
    assert flag(block, 'automerge') is False
    assert top_level_object(PATCHES_OFF, 'major') is None


def test_a_top_level_key_is_one_the_outermost_object_sets() -> None:
    """A rule's key of the same name is no top-level key, and a quoted name is the name."""
    config = """{
  packageRules: [
    {
      patch: 'not the top level',
    },
  ],
  'patch': {
    enabled: false,
  },
}
"""
    block = top_level_object(config, 'patch')
    assert block is not None
    assert flag(block, 'enabled') is False
    assert top_level_object(config.replace("'patch': {", 'minor: {'), 'patch') is None


@pytest.mark.parametrize(
    'setting',
    ['patch: { enabled: false },', 'patch: { prBodyDefinitions: { Notes: "" } },', 'patch: true,'],
    ids=['one-line', 'nesting-an-object', 'no-object'],
)
def test_a_top_level_object_the_scan_cannot_read_is_refused(setting: str) -> None:
    """A block read as setting nothing would read as patches left on, so one the scan cannot read fails."""
    config = f'{{\n  packageRules: [\n  ],\n  {setting}\n}}\n'
    with pytest.raises(Unread):
        _ = enabled(config, {'matchDepNames': ('talosctl',), 'matchUpdateTypes': ('patch',)})


def test_an_update_type_is_kept_as_renovate_merges_the_rules_over_its_object() -> None:
    """A matching rule setting `enabled` beats the update type's object from anywhere in the rules."""
    assert enabled(PATCHES_OFF, {'matchDepNames': ('talosctl',), 'matchUpdateTypes': ('patch',)})
    assert not enabled(PATCHES_OFF, {'matchDepNames': ('kubectl',), 'matchUpdateTypes': ('patch',)})
    assert enabled(PATCHES_OFF, {'matchDepNames': ('kubectl',), 'matchUpdateTypes': ('minor',)})
    later = PATCHES_OFF.replace(
        '  ],\n  patch:',
        "    {\n      matchUpdateTypes: ['patch'],\n      enabled: false,\n    },\n  ],\n  patch:",
    )
    assert not enabled(later, {'matchDepNames': ('talosctl',), 'matchUpdateTypes': ('patch',)})


def test_a_rule_is_proven_to_match_only_by_matchers_each_settled_on_a_value_presented() -> None:
    rule = """  {
    matchDepNames: ['talosctl', '{kubectl,cmctl}'],
    matchUpdateTypes: ['patch'],
  }"""
    patch = {'matchDepNames': ('talosctl',), 'matchUpdateTypes': ('patch',)}
    assert provably_matches(rule, patch)
    assert not provably_matches(rule, {'matchDepNames': ('talosctl',)})
    assert not provably_matches(rule, {**patch, 'matchDepNames': ('kubectl',)})


def test_a_glob_proves_a_match_only_without_a_wildcard() -> None:
    """`fnmatch` matches `*` across a `/`, which `minimatch` does not, so only a glob's miss settles through it."""
    path = 'src/kluster/lib/state_backend/settings.py'
    wildcard = "  {\n    matchFileNames: ['src/*/settings.py'],\n  }"
    assert reaches(wildcard, {'matchFileNames': (path,)})
    assert not provably_matches(wildcard, {'matchFileNames': (path,)})
    assert not reaches(wildcard, {'matchFileNames': ('docs/settings.md',)})
    assert provably_matches(f"  {{\n    matchFileNames: ['{path}'],\n  }}", {'matchFileNames': (path,)})
    assert provably_matches("  {\n    matchFileNames: ['*'],\n  }", {'matchFileNames': (path,)})


@pytest.mark.parametrize(
    'setting',
    ["matchCurrentVersion: '<1.0.0',", "excludeDepNames: ['kubectl'],", "matchDepNames: ['!kubectl'],"],
    ids=['a-matcher-not-presented', 'an-exclusion', 'a-negated-pattern'],
)
def test_a_rule_that_might_not_apply_does_not_decide_whether_an_update_is_kept(setting: str) -> None:
    """The last rule setting `enabled` that the reading cannot clear decides only where it provably applies."""
    config = PATCHES_OFF.replace('      enabled: true,\n', f'      {setting}\n      enabled: true,\n')
    assert config != PATCHES_OFF
    rule = package_rules(config)[0]
    patch = {'matchDepNames': ('talosctl',), 'matchUpdateTypes': ('patch',)}
    assert reaches(rule, patch)
    assert not provably_matches(rule, patch)
    with pytest.raises(Unread):
        _ = enabled(config, patch)


@pytest.mark.parametrize(
    ('written', 'value'),
    [
        (r'\\n', '\\n'),
        (r'\n', '\n'),
        (r'\t', '\t'),
        (r'\u0041', 'A'),
        (r'\x41', 'A'),
        (r'it\'s', "it's"),
        ('a\\\nb', 'ab'),
    ],
    ids=['an-escaped-backslash', 'a-line-feed', 'a-tab', 'a-u-escape', 'an-x-escape', 'a-quote', 'a-continued-line'],
)
def test_a_string_is_read_as_its_json5_value(written: str, value: str) -> None:
    assert string_value(written) == value


@pytest.mark.parametrize(
    ('written', 'admits', 'refuses'),
    [(r"'/^5\\.3\\./'", '5.3.1', '5.30.0'), (r"'/^v1\\.36\\./i'", 'V1.36.2', 'v1.37.0')],
    ids=['an-escaped-regex', 'a-final-i'],
)
def test_a_bound_is_the_regex_renovate_reads(written: str, admits: str, refuses: str) -> None:
    bound = allowed_versions(f'  {{\n    allowedVersions: {written},\n  }}')
    assert bound is not None
    assert bound.search(admits)
    assert not bound.search(refuses)


def test_a_rule_without_a_bound_has_none() -> None:
    assert allowed_versions("  {\n    matchDepNames: ['talosctl'],\n  }") is None


@pytest.mark.parametrize(
    ('written', 'failure'),
    [
        ("'<2.0.0'", 'is not a pattern'),
        ("'!/^2\\\\./'", 'is negated'),
        ("'/^{{major}}\\\\./'", 'is a template'),
        ("'/^(?!2\\\\.)/'", 'is no regex this reading holds'),
    ],
    ids=['range', 'negated', 'template', 're2-refuses'],
)
def test_a_bound_the_reading_does_not_hold_fails(written: str, failure: str) -> None:
    """A range is read in the dependency's versioning, a negation inverts, a template is expanded first.

    And a lookahead RE2 refuses leaves renovate with no regex, so it rejects the setting.
    """
    with pytest.raises(AssertionError, match=failure):
        _ = allowed_versions(f'  {{\n    allowedVersions: {written},\n  }}')


@pytest.mark.parametrize(
    ('version', 'following'),
    [('1.36.6', '1.37.0'), ('v1.36.3', '1.37.0'), ('2.9.1', '2.10.0'), ('5.3.1', '5.4.0'), ('17.11', '17.12.0')],
)
def test_the_next_line_is_the_next_minor(version: str, following: str) -> None:
    assert next_line(version) == following
