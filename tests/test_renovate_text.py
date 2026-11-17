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
from renovate_text import allowed_versions, effective, group, listed, matches, next_line, reaches, scalar

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
    assert listed(rule, 'matchDepNames') == []
    assert reaches(rule, {'matchDepNames': ('talosctl',)})


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
    assert listed(unreadable, 'matchFileNames') == []
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
    'written',
    ["'<2.0.0'", "'!/^2\\\\./'", "'/^{{major}}\\\\./'", "'/^(?!2\\\\.)/'"],
    ids=['range', 'negated', 'template', 're2-refuses'],
)
def test_a_bound_the_reading_does_not_hold_fails(written: str) -> None:
    """A range is read in the dependency's versioning, a negation inverts, a template is expanded first.

    And a lookahead RE2 refuses leaves renovate with no regex, so it rejects the setting.
    """
    with pytest.raises(AssertionError):
        _ = allowed_versions(f'  {{\n    allowedVersions: {written},\n  }}')


@pytest.mark.parametrize(
    ('version', 'following'),
    [('1.36.6', '1.37.0'), ('v1.36.3', '1.37.0'), ('2.9.1', '2.10.0'), ('5.3.1', '5.4.0'), ('17.11', '17.12.0')],
)
def test_the_next_line_is_the_next_minor(version: str, following: str) -> None:
    assert next_line(version) == following
