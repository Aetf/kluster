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
GITHUB_ADMIN = devices.DEVICES[devices.GITHUB_ADMIN]


def refuses(prompt: str) -> str:
    """A plain-value prompt that must never be reached."""
    raise AssertionError(f'nothing plain should have been asked for, and this was: {prompt}')


#: What a machine that holds every passphrase can tell a `pulumi` run. The
#: `github` stack is encrypted apart from the estate (`pulumi_config.APART`),
#: so a helper that left it out would have every case about that stack failing
#: on the passphrase instead of on its subject.
FULLY_EQUIPPED = pulumi_config.BackendEnvironment(
    passphrase='an-estate-passphrase',
    apart={stack: f'a-{stack}-passphrase' for stack in pulumi_config.APART},
)


def stack(name: str) -> tuple[pulumi_config.Stack, RecordedPulumi]:
    runner = RecordedPulumi()
    slot = pulumi_config.Stack(name=name, directory=pulumi_config.project_dir(), environment=FULLY_EQUIPPED, run=runner)
    return slot, runner


@pytest.fixture
def typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a secret prompt asks for, it gets `a-typed-secret`.

    Patched at `getpass` rather than injected, so what the test drives is the
    same door an operator types into -- and a field that stopped hiding its
    input would stop being covered by this fixture.
    """
    monkeypatch.setattr('getpass.getpass', lambda _prompt='': 'a-typed-secret')


def required(name: str) -> dict[str, bool]:
    """Every config key that stack's declaration requires, and whether it must travel encrypted.

    Parsed rather than imported: importing a stack program drags in the
    provider SDKs, and what this needs to know is a property of the source.

    The stack program is not the whole of the declaration. A credential that
    exists only to configure a provider is read at the line that builds that
    provider, which for a provider a single component owns is inside the
    component (rfc-002 §8.1) and for a dynamic provider is its `configure`
    (§7.4) -- so the components and the custom providers are searched too. That
    makes the answer the union over the tree rather than per stack, which is
    exact enough here because no two stacks name a key the same way and each
    device row asks only about its own keys.

    The two readers say "encrypted" differently. A stack program or a component
    holds a `pulumi.Config` and chooses the channel by which method it calls, so
    `require_secret` is the answer. A dynamic provider's `configure` receives
    the stack's configuration already decrypted and has no such choice -- and
    every key it may read is a credential by construction, everything a caller
    decides being a declared resource input instead, so a key read there is one
    that must travel encrypted.
    """
    root = pulumi_config.project_dir() / 'src' / 'kluster'
    programs = [root / 'stacks' / f'{name}.py', *sorted((root / 'components').rglob('*.py'))]
    providers = sorted((root / 'providers').rglob('*.py'))
    found: dict[str, bool] = {}
    for path in [*programs, *providers]:
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not node.func.attr.startswith('require') or not node.args:
                continue
            secret = path in providers or node.func.attr == 'require_secret'
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found[key.value] = secret
            elif isinstance(key, ast.Name):
                # The key is a module constant, which is how a component or a
                # provider names the credential it reads. Resolve it in that
                # module.
                found[_constant(path, key.id)] = secret
    return found


def _constant(path: Path, name: str) -> str:
    """The string a module-level assignment binds `name` to.

    Only in the module that reads it, which is the convention every credential
    key here follows: the component that reads a key names it beside itself. A
    key named by a constant *imported* from elsewhere is not resolved and
    raises rather than being silently dropped, so the contract this file holds
    cannot quietly stop covering a key.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return str(node.value.value)
    raise AssertionError(f'{name} is not a string constant of {path}')


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


def test_every_delivered_field_takes_the_encrypted_channel(typed: None) -> None:
    slot, runner = stack(ZEROTIER.stack)

    _ = devices.deliver(ZEROTIER, stack=slot)

    # Which channel each key takes is the assertion, not merely that the value
    # arrived: a value pushed in the clear and read as a secret agrees only by
    # way of an upstream defect. Every field on every row today is a
    # credential -- the one identifier a row used to carry beside one, the
    # overlay network's id, is a constant in `conventions` (rfc-002 §11).
    secret = [args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' in args]
    plain = [args[2] for args in runner.invocations if args[:2] == ['config', 'set'] and '--secret' not in args]
    assert secret == ['zerotierApiToken']
    assert plain == []


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


def test_a_value_handed_in_under_a_name_no_field_has_is_refused() -> None:
    slot, _ = stack(UNIFI.stack)

    # Dropping it silently turns a scripted run into an interactive one, at
    # exactly the prompt the caller meant to answer.
    with pytest.raises(KdbxError, match='no field named apikey'):
        _ = devices.deliver(UNIFI, stack=slot, given={'apikey': 'a-value'})


def test_the_delivery_names_the_file_to_commit(typed: None, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    slot, _ = stack(ADGUARD.stack)

    _ = devices.deliver(ADGUARD, stack=slot)

    # A push that stopped at `pulumi config set` would leave the credential
    # live on the device and invisible to everyone else's checkout.
    assert f'commit Pulumi.{ADGUARD.stack}.yaml' in caplog.text


def test_a_row_read_back_answers_with_what_was_delivered(typed: None) -> None:
    """The one row a command authenticates with, out of the stack that holds it.

    `credentials derived sync` pushes every GitHub secret as the admin token,
    and it takes it from the same place the `github` stack does rather than
    from a copy of its own — one credential, one home (credentials.md §3).
    """
    slot, _ = stack(GITHUB_ADMIN.stack)
    _ = devices.deliver(GITHUB_ADMIN, stack=slot)

    assert devices.borrow(GITHUB_ADMIN, stack=slot) == 'a-typed-secret'


def test_reading_back_a_stack_that_has_no_such_key_names_the_command_that_fills_it() -> None:
    """The mid-crossing state: a checkout whose stack file predates the key.

    `pulumi`'s own refusal says to run `pulumi config set`, which would put the
    credential in the process table and skip the read-back every config slot is
    delivered with. Naming the `record` command is what keeps the answer in the
    error rather than in a second investigation.
    """
    slot, _ = stack(GITHUB_ADMIN.stack)

    with pytest.raises(pulumi_config.SlotRefused, match=f'credentials derived {GITHUB_ADMIN.member} record'):
        _ = devices.borrow(GITHUB_ADMIN, stack=slot)


def test_a_row_that_reads_back_empty_is_refused_rather_than_handed_on() -> None:
    """An empty credential authenticates as nobody, and fails somewhere else.

    A slot emptied by hand is the way this happens; passing it on would make
    the failure a provider's refusal in whatever command borrowed it.
    """
    slot, runner = stack(GITHUB_ADMIN.stack)
    runner.config[GITHUB_ADMIN.fields[0].key] = '  '

    with pytest.raises(pulumi_config.SlotRefused, match='empty'):
        _ = devices.borrow(GITHUB_ADMIN, stack=slot)


def test_a_row_of_several_secrets_has_no_single_value_to_authenticate_with() -> None:
    """`borrow` answers "what does this authenticate as", which a pair cannot.

    A row with two secrets would have to say which, and the caller asking is
    asking for *the* credential — so the refusal is the honest answer rather
    than a choice made silently.
    """
    slot, _ = stack(ADGUARD.stack)

    with pytest.raises(KdbxError, match='no single value'):
        _ = devices.borrow(ADGUARD, stack=slot)


def test_a_stack_encrypted_apart_refuses_on_a_machine_that_holds_no_passphrase_for_it() -> None:
    """The trap the per-stack passphrase creates, closed where it is created.

    `PULUMI_CONFIG_PASSPHRASE` is process-global, so "a different passphrase
    for one stack" is a property of how that stack is invoked. Left to
    `pulumi`, the wrong one is answered with `error: incorrect passphrase` --
    loud, but naming neither the stack nor the fix, and arriving at the far end
    of whatever command was running. This refusal comes first and names both.
    """
    runner = RecordedPulumi()
    bare = pulumi_config.Stack(
        name=GITHUB_ADMIN.stack,
        directory=pulumi_config.project_dir(),
        environment=pulumi_config.BackendEnvironment(passphrase='an-estate-passphrase'),
        run=runner,
    )

    # The row is read off the census that decides it rather than typed here: a
    # rename moves both, where a literal would go on matching a message that
    # had stopped naming a command that exists (`docs/style/testing.md`).
    fills = pulumi_config.APART[GITHUB_ADMIN.stack]
    with pytest.raises(pulumi_config.PassphraseMissing, match=f'credentials derived {fills} generate'):
        _ = devices.borrow(GITHUB_ADMIN, stack=bare)

    # And the estate passphrase is not quietly used instead, which is the whole
    # point: that value is in every CI Environment.
    assert runner.invocations == []


def test_a_machine_that_cannot_decrypt_the_stack_is_not_told_the_credential_is_missing() -> None:
    """Two states, two answers. `borrow` dresses one refusal in its own words and not this one.

    "Run `record` to put the token there" sends an operator to the GitHub UI;
    what they actually need is the passphrase that opens the stack they would
    be writing into.
    """
    bare = pulumi_config.Stack(
        name=GITHUB_ADMIN.stack,
        directory=pulumi_config.project_dir(),
        environment=pulumi_config.BackendEnvironment(passphrase='an-estate-passphrase'),
        run=RecordedPulumi(),
    )

    with pytest.raises(pulumi_config.SlotRefused) as refusal:
        _ = devices.borrow(GITHUB_ADMIN, stack=bare)

    assert f'credentials derived {GITHUB_ADMIN.member} record' not in str(refusal.value)


def test_a_stack_on_the_estate_passphrase_is_handed_that_one() -> None:
    """The other half: only the stacks the census names are apart."""
    equipped = pulumi_config.Stack(
        name=devices.DNS_STACK, directory=pulumi_config.project_dir(), environment=FULLY_EQUIPPED
    )
    apart = pulumi_config.Stack(
        name=GITHUB_ADMIN.stack, directory=pulumi_config.project_dir(), environment=FULLY_EQUIPPED
    )

    assert devices.DNS_STACK not in pulumi_config.APART
    assert equipped.env[pulumi_config.PASSPHRASE_ENV] == 'an-estate-passphrase'
    assert apart.env[pulumi_config.PASSPHRASE_ENV] == f'a-{GITHUB_ADMIN.stack}-passphrase'
