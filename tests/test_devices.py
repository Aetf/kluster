"""The device credentials: console steps printed, value taken, config slot filled.

Against a recorded `pulumi`, because what is under test is the shape of the
procedure -- say where the credential comes from, obtain it without echoing it,
deliver it into the stack that reads it -- and the contract that shape rests
on: every key a device row writes is a key its consumer program requires, read
as a secret or in plain text exactly as it was pushed. That last one is read
out of the stack's own source, so a program that renames a key or changes how
it reads one fails here rather than at an operator's first `up`.
"""

from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest
from fake_pulumi import RecordedPulumi

from kluster.scripts.credentials import devices, pulumi_config
from kluster.scripts.credentials.kdbx import KdbxError

UNIFI = devices.DEVICES['unifi']
ADGUARD = devices.DEVICES['adguard']
ZEROTIER = devices.DEVICES['zerotier']
NETWORK_ID = '0123456789abcdef'


def refuses(prompt: str) -> str:
    """A plain-value prompt that must never be reached."""
    raise AssertionError(f'nothing plain should have been asked for, and this was: {prompt}')


def stack(name: str) -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    return pulumi_config.Stack(name=name, directory=pulumi_config.project_dir(), run=runner), runner


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a secret prompt asks for, it gets `a-typed-secret`.

    Patched at `getpass` rather than injected, so what the test drives is the
    same door an operator types into -- and a field that stopped hiding its
    input would stop being covered by this fixture.
    """
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': 'a-typed-secret')


def required(name: str) -> dict[str, bool]:
    """Every config key that stack's program requires, and whether it reads it as a secret.

    Parsed rather than imported: importing a stack program drags in the
    provider SDKs, and what this needs to know is a property of the source.
    """
    source = (pulumi_config.project_dir() / 'src' / 'kluster' / 'stacks' / f'{name}.py').read_text()
    found: dict[str, bool] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not node.func.attr.startswith('require') or not node.args:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            found[key.value] = node.func.attr == 'require_secret'
    return found


@pytest.mark.parametrize('member', sorted(devices.DEVICES), ids=sorted(devices.DEVICES))
def test_every_field_is_a_key_the_consumer_program_reads_the_same_way(member: str) -> None:
    device = devices.DEVICES[member]

    reads = required(device.stack)

    # Both halves of the contract at once: the key exists in the program that
    # consumes it, and the channel matches. A value pushed as a secret and read
    # plain agrees only by way of an upstream defect (pulumi/pulumi#7127), so
    # the two sides have to be made to say the same thing.
    delivered = {field.key: field.secret for field in device.fields}
    assert {key: reads.get(key) for key in delivered} == delivered


@pytest.mark.parametrize('member', sorted(devices.DEVICES), ids=sorted(devices.DEVICES))
def test_every_field_is_addressable_from_the_command_line(member: str) -> None:
    device = devices.DEVICES[member]

    flags = [field.flag for field in device.fields]

    # A secret is handed in as a path and never as a value -- an argument would
    # put the credential in the process table of a shared machine -- and the
    # `-file` suffix is what says so at the command line.
    assert len(set(flags)) == len(flags)
    assert [flag.endswith('-file') for flag in flags] == [field.secret for field in device.fields]
    assert [field.dest for field in device.fields] == [flag.removeprefix('--').replace('-', '_') for flag in flags]


def test_the_console_steps_are_printed_before_the_value_is_asked_for(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    slot, runner = stack(UNIFI.stack)
    asked: list[str] = []

    def watching(_prompt: str = '') -> str:
        asked.append(caplog.text)
        return 'an-api-key'

    monkeypatch.setattr('getpass.getpass', watching)

    # The delivery is the key alone: the controller's address is a constant
    # the consuming stack derives, so a run that asked for it in plain text
    # would be recording a second copy of something already stated.
    _ = devices.deliver(UNIFI, stack=slot, prompt=refuses)

    # The steps are the register's answer to "where does this come from", and
    # an operator asked for a value before being told where to get it has to go
    # and find it somewhere else.
    (before,) = asked
    for line in UNIFI.console.splitlines():
        assert line.strip() in before
    assert runner.config == {'unifiApiKey': 'an-api-key'}


def test_the_typed_values_land_in_the_stack_the_row_names(typed: None) -> None:
    slot, runner = stack(ADGUARD.stack)

    keys = devices.deliver(ADGUARD, stack=slot)

    # Which stack is a property of the row: the credential authenticates
    # against one device, and one stack talks to that device.
    assert keys == ('adguardUsername', 'adguardPassword')
    assert runner.config == {'adguardUsername': 'a-typed-secret', 'adguardPassword': 'a-typed-secret'}


def test_a_secret_field_is_encrypted_and_a_plain_one_is_not(typed: None) -> None:
    slot, runner = stack(ZEROTIER.stack)

    _ = devices.deliver(ZEROTIER, stack=slot, given={'network-id': NETWORK_ID})

    # Which channel each key takes is the assertion, not merely that the value
    # arrived: the network id is an identifier the committed file may carry in
    # the clear, and the token it travels with is not.
    secret = [args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' in args]
    plain = [args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' not in args]
    assert secret == ['zerotierApiToken']
    assert plain == ['zerotierNetworkId']


def test_the_stack_is_created_when_the_backend_has_none(typed: None) -> None:
    slot, runner = stack(UNIFI.stack)

    _ = devices.deliver(UNIFI, stack=slot)

    # A workstation that has never selected this stack is the ordinary case at
    # bring-up, so the push cannot assume one exists.
    assert runner.stacks == [UNIFI.stack]


def test_a_push_that_does_not_read_back_is_refused(typed: None) -> None:
    slot, runner = stack(ADGUARD.stack)
    runner.corrupts = True

    # The file gains ciphertext either way; decrypting it again is the only
    # thing that tells a delivered credential from a corrupted one.
    with pytest.raises(pulumi_config.SlotRefused):
        _ = devices.deliver(ADGUARD, stack=slot)


def test_a_value_in_a_file_is_delivered_without_a_prompt(tmp_path: Path) -> None:
    slot, runner = stack(UNIFI.stack)
    key = tmp_path / 'api-key'
    # A trailing newline is what any editor leaves behind, and a config value
    # carrying one can never compare equal to itself on read-back.
    _ = key.write_text('an-api-key\n')

    _ = devices.deliver(UNIFI, stack=slot, given={'api-key': str(key)}, prompt=refuses)

    # Nothing is patched at `getpass` here: a run that asked for anything would
    # fail rather than pass.
    assert runner.config == {'unifiApiKey': 'an-api-key'}


def test_a_value_can_be_piped_in(monkeypatch: pytest.MonkeyPatch) -> None:
    slot, runner = stack(UNIFI.stack)
    monkeypatch.setattr('sys.stdin', io.StringIO('a-piped-key\n'))

    _ = devices.deliver(UNIFI, stack=slot, given={'api-key': devices.STDIN}, prompt=refuses)

    assert runner.config['unifiApiKey'] == 'a-piped-key'


def test_an_answer_left_blank_is_refused_and_nothing_is_pushed(monkeypatch: pytest.MonkeyPatch) -> None:
    slot, runner = stack(ADGUARD.stack)
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': '   ')

    with pytest.raises(KdbxError, match='is required'):
        _ = devices.deliver(ADGUARD, stack=slot)

    # Every value is collected before the first one is pushed, so a run
    # abandoned at a prompt leaves the stack as it was rather than half filled.
    assert runner.invocations == []


def test_a_file_whose_producer_failed_is_refused_by_name(tmp_path: Path, typed: None) -> None:
    empty = tmp_path / 'api-key'
    _ = empty.write_text('\n')
    slot, _ = stack(UNIFI.stack)

    # An empty file is what a failed producer leaves behind, and a credential
    # delivered as an empty string fails much later, in a stack nobody is
    # watching.
    with pytest.raises(KdbxError, match='came through empty'):
        _ = devices.deliver(UNIFI, stack=slot, given={'api-key': str(empty)})


def test_the_delivery_names_the_file_to_commit(typed: None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    slot, _ = stack(ADGUARD.stack)

    _ = devices.deliver(ADGUARD, stack=slot)

    # A push that stopped at `pulumi config set` would leave the credential
    # live on the device and invisible to everyone else's checkout.
    assert f'commit Pulumi.{ADGUARD.stack}.yaml' in caplog.text
