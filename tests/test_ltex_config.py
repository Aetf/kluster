"""The configuration `ltex-config` writes carries both word lists to the checker.

The checker reads neither list on its own, and drops a reference to a list
file in silence, so a configuration that lost either list would read as a
run with nothing to say about it (AGENTS.md, "Writing the prose"). The case
holds the configuration by what the pinned `ltex-cli-plus` makes of it, in
one run over two copies of a document holding one word only the dictionary
admits and one finding only a disabled rule raises. The first copy is
checked as `en-US`, the language both lists are keyed by, and reports
nothing. The second opens with the checker's magic comment for `en-GB`,
which no list of the configuration is keyed by, and reports both: that is
what shows the document raises them at all, in the same run and under the
same configuration, so the run starts the checker once.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import ltex_checker
import pinned_tools
import process_sessions
import pytest

from kluster.scripts import ltex_config

#: A word no dictionary but this one holds, written with the capital the
#: entry has, since the dictionary is case-sensitive.
WORD = 'Frobnicatorix'
#: The rule the repeated word below raises.
RULE = 'ENGLISH_WORD_REPEAT_RULE'
#: The rule `en-GB` raises on a word outside its dictionary.
SPELLING = 'MORFOLOGIK_RULE_EN_GB'
DOCUMENT = f'# Notes\n\nThe {WORD} is is ready.\n'
#: The magic comment that has the checker read the rest of a file as `en-GB`.
EN_GB = '<!-- LTeX: language=en-GB -->\n\n'

#: `ltex-cli-plus`'s exit when it reports a finding.
FINDINGS = 3


def _rules(output: str, document: Path) -> list[str]:
    """The rule of each finding the checker reported on `document`."""
    return re.findall(rf'^{re.escape(str(document))}:\d+:\d+: \w+: .*\[(\w+)\]$', output, re.M)


def test_the_checker_honors_both_lists_under_the_configuration_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / '.vscode').mkdir()
    _ = ltex_config.word_list(tmp_path, 'dictionary').write_text(f'Homelab\n{WORD}\nhomelab\n')
    _ = ltex_config.word_list(tmp_path, 'disabledRules').write_text(f'GITHUB {RULE}\n')
    listed = tmp_path / 'listed.md'
    _ = listed.write_text(DOCUMENT)
    control = tmp_path / 'control.md'
    _ = control.write_text(EN_GB + DOCUMENT)
    monkeypatch.chdir(tmp_path)

    assert ltex_config.main(['ltex.json']) == 0

    argv = [pinned_tools.located('ltex-cli-plus'), '--client-configuration=ltex.json', str(listed), str(control)]
    # Empty, the archive's launcher runs the JRE it bundles, which is the pinned one.
    done = process_sessions.run(argv, env={**os.environ, 'JAVA_HOME': ''}, text=True, timeout=ltex_checker.STOP_LOSS)
    output = done.stdout + done.stderr
    assert done.returncode == FINDINGS, output
    assert _rules(output, listed) == [], output
    assert sorted(_rules(output, control)) == sorted([SPELLING, RULE]), output
