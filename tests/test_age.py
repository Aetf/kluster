"""The age wrapper is checked against age itself.

There is nothing to unit-test here in isolation: the module is a shell around
the tool that owns the format, so what is worth asserting is that the round
trip works, that a wrong key is a refusal rather than a wrong plaintext, and
that the pin the appliance downloads is the pin this suite exercises.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from kluster.scripts.credentials import age

age_binary = shutil.which(age.BINARY)
needs_age = pytest.mark.skipif(age_binary is None, reason='age is not on PATH (mise x -- ...)')


@needs_age
def test_an_identity_round_trips(tmp_path: Path) -> None:
    identity = age.generate()
    path = tmp_path / 'secret.age'

    _ = path.write_text(age.encrypt('hunter2', [identity.public]))

    assert age.decrypt(path, [identity.secret]) == 'hunter2'


@needs_age
def test_the_public_half_is_the_one_age_computes() -> None:
    identity = age.generate()

    assert identity.secret.startswith(age.SECRET_PREFIX)
    assert identity.public.startswith(age.PUBLIC_PREFIX)
    assert age.recipient(identity.secret) == identity.public


@needs_age
def test_two_identities_are_two_identities() -> None:
    assert age.generate().public != age.generate().public


@needs_age
def test_a_file_is_armoured_and_recognised_as_one(tmp_path: Path) -> None:
    # The escrow's `check` runs without a key, so "is this an age file at all"
    # has to be answerable from the bytes.
    armoured = age.encrypt('x', [age.generate().public])

    assert age.is_armoured(armoured)
    assert not age.is_armoured('-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----')


@needs_age
def test_any_of_several_identities_opens_it(tmp_path: Path) -> None:
    # What makes a re-wrap resumable: the caller does not have to know which
    # key a given file is currently under.
    wanted = age.generate()
    other = age.generate()
    path = tmp_path / 'secret.age'
    _ = path.write_text(age.encrypt('hunter2', [wanted.public]))

    assert age.decrypt(path, [other.secret, wanted.secret]) == 'hunter2'


@needs_age
def test_the_wrong_identity_is_a_refusal(tmp_path: Path) -> None:
    path = tmp_path / 'secret.age'
    _ = path.write_text(age.encrypt('hunter2', [age.generate().public]))

    with pytest.raises(age.AgeError):
        _ = age.decrypt(path, [age.generate().secret])


@needs_age
def test_multiple_recipients_all_open_it(tmp_path: Path) -> None:
    # The generational pair the appliance encrypts every dump to.
    current = age.generate()
    previous = age.generate()
    path = tmp_path / 'dump.age'
    _ = path.write_text(age.encrypt('backup', [current.public, previous.public]))

    assert age.decrypt(path, [current.secret]) == 'backup'
    assert age.decrypt(path, [previous.secret]) == 'backup'


def test_encrypting_to_nobody_is_refused() -> None:
    with pytest.raises(age.AgeError, match='no recipient'):
        _ = age.encrypt('x', [])


def test_decrypting_with_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(age.AgeError, match='no identity'):
        _ = age.decrypt(tmp_path / 'absent.age', [])


def test_a_missing_tool_names_where_it_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    # A headless run without mise's tools should say what to do, not raise a
    # FileNotFoundError from three frames down.
    monkeypatch.setattr(age, 'BINARY', 'age-that-is-not-installed')

    with pytest.raises(age.AgeError, match='mise.toml'):
        _ = age.decrypt(Path('irrelevant'), ['AGE-SECRET-KEY-1'])


def test_age_url_matches_the_pinned_version() -> None:
    """A version bumped without its URL would fetch the old binary and pass
    its own digest check."""
    from kluster.scripts.state_backend import settings

    assert settings.AGE_VERSION in settings.AGE_URL
    assert settings.AGE_URL.endswith('linux-amd64.tar.gz')
    assert len(settings.AGE_SHA256) == 64


def test_local_age_matches_the_appliance_pin() -> None:
    """The round trip is only worth as much as the tool that runs it.

    `mise.toml`'s age is what this suite exercises; the appliance runs the one
    `settings.AGE_VERSION` pins. Letting them drift would leave the
    appliance's version untested.
    """
    import tomllib

    from kluster.scripts.state_backend import settings

    tools = tomllib.loads((Path(__file__).parent.parent / 'mise.toml').read_text())['tools']
    assert f'v{tools["age"]}' == settings.AGE_VERSION


def package_rule(config: str, at: int) -> tuple[int, str]:
    """The `packageRules` entry around offset `at`: where it starts, and its lines bar comments.

    An entry is the braces around the offset, which holds because no entry
    nests an object and no comment inside one carries a brace.
    """
    start = config.rindex('{', 0, at)
    lines = config[start : config.index('}', at)].splitlines()
    return start, '\n'.join(line for line in lines if not line.lstrip().startswith('//'))


def listed(rule: str, key: str) -> list[str]:
    """The strings a rule lists under `key`, or none when it has no such key."""
    found = re.search(rf'^\s*{key}: \[([^\]]*)\],$', rule, re.MULTILINE)
    return re.findall(r"'([^']*)'", found[1]) if found else []


def group(rule: str) -> list[str]:
    """The group name and slug a rule sets, in the order it writes them."""
    return re.findall(r"^\s*group(?:Name|Slug): '([^']*)',$", rule, re.MULTILINE)


def test_renovate_bumps_both_pins_in_one_pull_request() -> None:
    """The pair the test above holds equal has to move in one renovate pull request.

    Apart, each half fails that test on its own. The mise manager reads
    `mise.toml`, and the rule matching that manager puts every tool in the
    toolchain group; the rule taking age back out has to come after that one
    — the last match wins a field — match that one dependency of that one
    manager, spelled the way `mise.toml` keys it, and name the group the rule
    for `settings.py` names. The slug is the branch, so a slug of its own is a
    second pull request again, and nothing goes red on a pull request for it:
    renovate reads its configuration from the default branch.

    Held as text, because there is no JSON5 parser here.
    """
    import tomllib

    from kluster.scripts.state_backend import settings

    root = Path(__file__).parent.parent
    config = (root / 'renovate.json5').read_text()
    appliance = "'src/kluster/scripts/state_backend/settings.py'"
    assert config.count(appliance) == 1
    _, appliance_rule = package_rule(config, config.index(appliance))

    # Two entries name the mise manager: the toolchain rule matches all of it,
    # and the rule for age narrows it to one dependency.
    mise = [package_rule(config, found.start()) for found in re.finditer("'mise'", config)]
    (toolchain_start,) = [start for start, rule in mise if not listed(rule, 'matchDepNames')]
    ((age_start, age_rule),) = [(start, rule) for start, rule in mise if listed(rule, 'matchDepNames')]
    assert toolchain_start < age_start

    # Renovate tells the keys a rule matches on from the fields it applies by
    # their prefix, and a matcher beyond these two would narrow the rule.
    keys = re.findall(r'^\s*(\w+):', age_rule, re.MULTILINE)
    assert [key for key in keys if key.startswith(('match', 'exclude'))] == ['matchManagers', 'matchDepNames']
    assert listed(age_rule, 'matchManagers') == ['mise']

    # The mise manager names a tool by its key under `[tools]`, a backend
    # prefix included, so the rule matches only while it spells that key.
    (tool,) = listed(age_rule, 'matchDepNames')
    tools = tomllib.loads((root / 'mise.toml').read_text())['tools']
    assert f'v{tools[tool]}' == settings.AGE_VERSION

    assert len(group(appliance_rule)) == 2
    assert group(age_rule) == group(appliance_rule)
