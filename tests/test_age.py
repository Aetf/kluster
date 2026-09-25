"""The age wrapper is checked against age itself.

There is nothing to unit-test here in isolation: the module is a shell around
the tool that owns the format, so what is worth asserting is that the round
trip works, that a wrong key is a refusal rather than a wrong plaintext, and
that the pin the appliance downloads is the pin this suite exercises.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
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


@needs_age
def test_a_recipient_is_whatever_the_tool_parses_as_one() -> None:
    age.check_recipient(age.generate().public, name='line 1')


def _mistyped(public: str) -> str:
    """`public` with its last six characters replaced: the prefix survives and the checksum does not."""
    return f'{public[:-6]}{"qqqqqq" if not public.endswith("qqqqqq") else "pppppp"}'


def _not_recipients() -> list[str]:
    """Lines that are not recipients, some carrying characters a quoting echo would escape."""
    public = age.generate().public
    return [_mistyped(public), 'age1yubikey1qqqq', 'not a recipient', f'"{public}"', f'{public}\\', 'ssh-ed25519 AAAA']


@needs_age
def test_a_line_that_is_not_a_recipient_is_refused_by_name_alone() -> None:
    # Whatever the tool's reason, the refusal names the line and carries no
    # part of it -- neither as given nor as an echo that escaped its quotes.
    for value in _not_recipients():
        with pytest.raises(age.AgeError) as refused:
            age.check_recipient(value, name='line 4')

        said = str(refused.value)
        assert said.startswith('line 4 is not an age recipient'), said
        assert value not in said
        assert json.dumps(value)[1:-1] not in said


def _identities() -> list[str]:
    """One private key in each shape it is pasted in: bare, lower-cased, quoted, assigned, a JSON member."""
    secret = age.generate().secret
    return [secret, secret.lower(), f'"{secret}"', f'SOPS_AGE_KEY="{secret}"', f'"key": "{secret}",']


@needs_age
def test_a_refusal_reads_as_the_line_and_the_tools_reason() -> None:
    # The reason without the tool's wrapping: the recipients file it read was
    # standard input, and its position there is the line's own name already.
    with pytest.raises(age.AgeError) as refused:
        age.check_recipient(_mistyped(age.generate().public), name='line 4')

    assert str(refused.value) == 'line 4 is not an age recipient (malformed recipient)'


@needs_age
def test_the_position_the_tool_gives_is_left_out_of_its_verdict() -> None:
    # A line that looks like an identity of some other kind gets past the
    # prefix refusal, and the tool's verdict on it carries the position it
    # read it at, which is the line's own name already.
    with pytest.raises(age.AgeError) as refused:
        age.check_recipient('AGE-PLUGIN-EXAMPLE-1QQQQ', name='line 2')

    assert str(refused.value) == 'line 2 is not an age recipient (apparent identity found in recipients file)'


#: The bech32 alphabet and generator, for a plugin recipient that parses.
_BECH32 = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _bech32(hrp: str, data: list[int]) -> str:
    """`data` under `hrp` with a valid checksum -- BIP 173, the encoding age's recipients use."""
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    check = 1
    for value in [*expanded, *data, 0, 0, 0, 0, 0, 0]:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for bit, generator in enumerate(_GENERATOR):
            check ^= generator if (top >> bit) & 1 else 0
    check ^= 1
    return f'{hrp}1' + ''.join(_BECH32[d] for d in [*data, *((check >> 5 * (5 - i)) & 31 for i in range(6))])


@needs_age
def test_a_failure_that_is_not_a_verdict_on_the_line_is_the_bare_refusal() -> None:
    # A well-formed plugin recipient parses, and fails only when the tool goes
    # to encrypt to it -- saying which plugin it could not find, which is a
    # piece of the line. Only a verdict given on a line of the file is kept.
    value = _bech32('age1s3cretpluginname', [0] * 20)

    with pytest.raises(age.AgeError) as refused:
        age.check_recipient(value, name='line 3')

    assert str(refused.value) == 'line 3 is not an age recipient'


def test_a_reason_that_repeats_the_value_is_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verdict on the line that echoes it, raw or escaped, is dropped.

    The pinned `age` names a bad line in a recipients file by its position
    only; a later one that did not would otherwise print the line.
    """
    echoing = tmp_path / age.BINARY
    _ = echoing.write_text(
        f'#!{sys.executable}\n'
        'import json, sys\n'
        'value = sys.stdin.read().strip()\n'
        'sys.stderr.write(f"age: error: failed to parse recipient file \\"-\\": \\"-\\": unknown {json.dumps(value)} ({value})\\n")\n'
        'sys.exit(1)\n'
    )
    echoing.chmod(0o755)
    monkeypatch.setattr(age, 'BINARY', str(echoing))
    value = 'not "a" recipient'

    with pytest.raises(age.AgeError) as refused:
        age.check_recipient(value, name='line 5')

    assert str(refused.value) == 'line 5 is not an age recipient'


@pytest.mark.parametrize('shape', range(5), ids=['bare', 'lower', 'quoted', 'assigned', 'json'])
def test_an_identity_is_refused_before_it_reaches_the_tool(shape: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Anywhere in the line and in any case: a key copied out of an env or JSON
    # file brings its quotes along. No tool is there to ask, so the refusal
    # cannot have come from one.
    value = _identities()[shape]
    monkeypatch.setattr(age, 'BINARY', 'age-that-is-not-installed')

    with pytest.raises(age.AgeError, match='private half') as refused:
        age.check_recipient(value, name='line 2')

    assert str(refused.value).startswith('line 2 ')
    assert age.SECRET_PREFIX not in str(refused.value).upper()


@needs_age
def test_no_line_reaches_the_tools_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the tool is asked, as the process table shows it.

    A stand-in `age` on PATH records its argv and hands over to the real one,
    so what is checked is what any process on the machine could have read
    while the check ran -- for a line that parses and for every kind that
    does not.
    """
    real = shutil.which(age.BINARY)
    assert real is not None
    shim = tmp_path / 'bin' / age.BINARY
    shim.parent.mkdir()
    log = tmp_path / 'argv.jsonl'
    _ = shim.write_text(
        f'#!{sys.executable}\n'
        'import json, os, sys\n'
        f'with open({str(log)!r}, "a") as f: f.write(json.dumps(sys.argv) + "\\n")\n'
        f'os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv('PATH', f'{shim.parent}{os.pathsep}{os.environ["PATH"]}')
    public = age.generate().public
    lines = [public, *_not_recipients(), *_identities()]

    for value in lines:
        try:
            age.check_recipient(value, name='line 1')
        except age.AgeError:
            pass

    recorded = log.read_text()
    assert len(recorded.splitlines()) == 1 + len(_not_recipients())
    for value in lines:
        assert value not in recorded
        assert json.dumps(value)[1:-1] not in recorded


def test_a_missing_tool_is_not_a_refused_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every line would read as wrong otherwise, which sends the operator to the
    # file instead of to the PATH.
    monkeypatch.setattr(age, 'BINARY', 'age-that-is-not-installed')

    with pytest.raises(age.AgeMissing, match='mise.toml'):
        age.check_recipient('age1anything', name='line 1')


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
