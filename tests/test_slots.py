"""The slot map, held against the register it mirrors, and the push that reads it.

Two halves. The first reads `docs/credentials.md` §3 and compares it with the
map: a credential in the table with no map row, or a map row naming a credential
the table does not, fails here — which is the only thing keeping two
descriptions of one inventory from drifting apart. The second drives
`slots.sync` against a `gh` that runs nothing, because what is under test is
which slots a row fills and what it says when it cannot fill one; the subprocess
itself is `test_github_secrets.py`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_gh import RecordedGh

from kluster.scripts.credentials import devices, escrow, pulumi_config, slots
from kluster.scripts.credentials.github_secrets import Forge, Slot
from kluster.scripts.credentials.pulumi_config import SlotRefused

REPOSITORY = slots.REPOSITORY
PASSPHRASE = 'a-recovered-passphrase'

#: §3 rows the map deliberately does not carry. Empty, and meant to stay that
#: way: a credential the register names is a credential something has to
#: deliver, so a new row belongs in the map with its `pending` reason rather
#: than in an exception list. An entry here needs a sentence saying why the row
#: can never have one.
UNMAPPED: frozenset[str] = frozenset()


def register_credentials() -> list[str]:
    """The first column of §3's table, in the document's own words.

    Read out of the file rather than copied here: a copy is a third description
    of the inventory, and the point of this test is that there are two.
    """
    document = (pulumi_config.project_dir() / 'docs' / 'credentials.md').read_text()
    section = document.split('\n## 3. ', 1)[1].split('\n## 4. ', 1)[0]
    rows = [line for line in section.splitlines() if line.startswith('| ')]
    cells = [line.split('|')[1].strip() for line in rows]
    return [cell for cell in cells if cell not in {'Credential'} and not cell.startswith('---')]


def test_every_map_row_names_a_credential_the_register_carries() -> None:
    named = set(register_credentials())

    unknown = {name: row.register for name, row in slots.ROWS.items() if row.register not in named}

    # A row naming something §3 does not is either a typo or a credential
    # introduced without its register row, which rule 3 forbids as one change.
    assert unknown == {}


def test_every_register_row_is_in_the_map() -> None:
    mapped = {row.register for row in slots.ROWS.values()}

    missing = [credential for credential in register_credentials() if credential not in mapped | UNMAPPED]

    assert missing == []


def test_the_map_targets_environments_the_forge_stack_declares() -> None:
    # Imported here rather than at module scope: `slots` must not depend on the
    # Pulumi provider SDKs, and this is the test that ties the two together
    # without letting the dependency into the command.
    from kluster.stacks import github

    declared = {*github.PREVIEWED_LAYERS, github.PLAN_ENVIRONMENT, github.APPLY_ENVIRONMENT}
    environments = {
        slot.environment for row in slots.ROWS.values() for slot in row.sinks if slot.environment is not None
    }

    assert slots.REPOSITORY == f'{github.OWNER}/{github.DEPLOYMENT_REPO}'
    assert slots.OPS_REPOSITORY == f'{github.OWNER}/{github.OPS_REPO}'
    assert set(slots.ENVIRONMENTS) == declared
    # A secret pushed into an Environment the stack does not declare is a
    # secret no job will ever see.
    assert environments <= declared


def test_the_passphrase_reaches_every_environment() -> None:
    # Every job runs a `pulumi` command, and both Pulumi channels are encrypted
    # under this one value, so a missing Environment here is a layer of the
    # merge chain that cannot start.
    passphrase = slots.ROWS['pulumi-passphrase']

    assert {slot.environment for slot in passphrase.sinks} == set(slots.ENVIRONMENTS)
    assert {slot.name for slot in passphrase.sinks} == {'PULUMI_CONFIG_PASSPHRASE'}


def test_a_device_row_advertises_the_keys_its_own_command_writes() -> None:
    for member, device in devices.DEVICES.items():
        row = slots.ROWS[member]

        # The map is built from the device table rather than restating it, for
        # the reason the minted rows import their key names: two descriptions
        # of one delivery are two things to keep in step.
        assert row.register == device.register
        assert row.targets == tuple(
            slots.PulumiConfig(device.stack, field.key, secret=field.secret) for field in device.fields
        )
        assert f'credentials derived {member} record' in row.source.describe()


def test_the_webhook_is_a_repository_secret_and_not_an_environment_one() -> None:
    # The job that reads it belongs to no stack, so an Environment secret would
    # be invisible to it (ci.md §3).
    (slot,) = slots.ROWS['haos-webhook'].sinks

    assert slot == Slot(repository=REPOSITORY, name='HAOS_DEPLOY_WEBHOOK_URL')


# --------------------------------------------------------------------------
# The push.
# --------------------------------------------------------------------------


@dataclass
class RecordedPulumi:
    """Just enough `pulumi` for a state read: which stacks exist, and what they hold."""

    stacks: list[str] = field(default_factory=list[str])
    outputs: dict[str, object] = field(default_factory=dict[str, object])
    config: dict[str, str] = field(default_factory=dict[str, str])
    invocations: list[list[str]] = field(default_factory=list[list[str]])

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        self.invocations.append(list(args))
        match list(args):
            case ['stack', 'ls', '--json']:
                return json.dumps([{'name': name} for name in self.stacks])
            case ['stack', 'output', '--json', '--show-secrets', '--stack', _]:
                return json.dumps(self.outputs)
            case ['config', 'get', key, '--stack', _]:
                return self.config[key] + '\n'
            case unknown:  # pragma: no cover - an invocation a read is not meant to make
                raise AssertionError(f'unexpected pulumi invocation {unknown}')


def unopened() -> escrow.Vault:
    """A kit nobody may open: reaching for one is the failure under test."""
    raise AssertionError('this push opened the kit, which the row it pushed does not need')


class Vault(escrow.Vault):
    """An escrow that recovers without a key, standing in for the kit's own."""

    def recover(self, label: str, generation: int | None = None) -> str:
        assert label in escrow.register() or label.startswith(escrow.BACKUP), label
        return PASSPHRASE


def opened() -> escrow.Vault:
    return Vault(registry=escrow.Registry(root=Path('nowhere')), identity='not-an-identity')


def typing_in(value: str) -> Callable[[str], str]:
    """An operator who answers every prompt with the same thing."""
    return lambda _prompt: value


def context(
    gh: RecordedGh,
    *,
    open_vault: Callable[[], escrow.Vault] = unopened,
    runner: pulumi_config.Runner | None = None,
    ask: Callable[[str], str] | None = None,
) -> slots.Context:
    """A push that reaches nothing real: no kit, no backend, no forge, no terminal."""
    return slots.Context(
        forge=Forge(token='the-account-root', run=gh),
        open_vault=open_vault,
        runner=runner if runner is not None else RecordedPulumi(),
        ask=ask if ask is not None else typing_in('typed-in'),
    )


def test_a_derived_row_is_recovered_once_and_pushed_to_every_slot() -> None:
    gh = RecordedGh()

    pushed = slots.sync(context(gh, open_vault=opened), only='pulumi-passphrase')

    # One recovery, five deliveries: the value is obtained once and fanned out,
    # so a rotation is one command rather than one per Environment.
    assert len(pushed) == len(slots.ENVIRONMENTS)
    for environment in slots.ENVIRONMENTS:
        assert gh.values[(REPOSITORY, environment, 'PULUMI_CONFIG_PASSPHRASE')] == PASSPHRASE


def test_a_push_verifies_through_the_listing_because_the_value_never_comes_back() -> None:
    gh = RecordedGh()

    _ = slots.sync(context(gh, open_vault=opened), only='pulumi-passphrase')

    # A secret is write-only, so the check is that the name is in the listing
    # and its timestamp moved — read before the push and again after it.
    listings = [args for args in gh.invocations if args[0:2] == ['secret', 'list']]
    assert len(listings) == 2 * len(slots.ENVIRONMENTS)


def test_a_slot_that_does_not_show_the_secret_afterwards_is_a_failure() -> None:
    gh = RecordedGh(forgets=True)

    with pytest.raises(SlotRefused, match='does not show it'):
        _ = slots.sync(context(gh, open_vault=opened), only='pulumi-passphrase')


def test_a_re_push_inside_one_second_is_a_warning_and_not_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    gh = RecordedGh()

    _ = slots.sync(context(gh, open_vault=opened), only='pulumi-passphrase')
    _ = slots.sync(context(gh, open_vault=opened), only='pulumi-passphrase')

    # Two runs in the same second share a timestamp. The name being there is
    # what distinguishes a delivered secret from a refused one, so this is
    # worth a word rather than a red run.
    assert 'the listing still reads' in caplog.text


def test_a_typed_in_row_is_asked_for_once_and_left_alone_afterwards(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    gh = RecordedGh()
    webhook = {'haos-webhook': slots.ROWS['haos-webhook']}

    _ = slots.sync(context(gh), only='haos-webhook')
    pushed = slots.sync(context(gh), rows=webhook)

    # A bring-up run walks the whole map; stopping to re-type a value that is
    # already in place would make that run interactive for no reason.
    assert gh.values[(REPOSITORY, None, 'HAOS_DEPLOY_WEBHOOK_URL')] == 'typed-in'
    assert pushed == []
    assert 'already in every slot' in caplog.text


def test_naming_a_typed_in_row_replaces_what_is_there() -> None:
    gh = RecordedGh(collections={(REPOSITORY, None): {'HAOS_DEPLOY_WEBHOOK_URL': '2026-01-01T00:00:00Z'}})

    pushed = slots.sync(context(gh, ask=typing_in('a-new-webhook')), only='haos-webhook')

    # Rotating it is a new webhook id in Home Assistant and this command, so
    # naming the row has to mean "replace" rather than "leave it".
    assert pushed
    assert gh.values[(REPOSITORY, None, 'HAOS_DEPLOY_WEBHOOK_URL')] == 'a-new-webhook'


def test_a_typed_in_row_that_is_left_empty_is_refused() -> None:
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='is required'):
        _ = slots.sync(context(gh, ask=typing_in('  ')), only='haos-webhook')


def test_the_kit_is_not_opened_for_a_row_that_does_not_need_it() -> None:
    # `Unopened` fails the test if it is called: pushing the one typed-in row
    # must not ask for a kit, an escrow or a state backend.
    gh = RecordedGh()

    _ = slots.sync(context(gh), only='haos-webhook')

    assert gh.values


def test_a_state_read_pushes_the_output_the_map_names() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'], outputs={'ci_zerotier_identity_dns': 'an-identity'})

    _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')

    # `--show-secrets`, because an identity is a secret output and without it
    # the string `[secret]` would be pushed as though it were the credential.
    assert gh.values[(REPOSITORY, 'dns', 'ZEROTIER_IDENTITY')] == 'an-identity'


def test_a_state_read_before_the_stack_exists_says_so() -> None:
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='has no state'):
        _ = slots.sync(context(gh, runner=RecordedPulumi()), only='zerotier-identity-dns')


def test_a_state_read_of_an_output_the_program_does_not_export_says_so() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'])

    # The output name is this map's half of a contract the program has to keep,
    # so an unkept one is named rather than pushed as an empty secret.
    with pytest.raises(SlotRefused, match='exports no'):
        _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')


def test_the_network_id_is_read_from_the_stack_that_declares_it() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'], config={'zerotierNetworkId': 'a-network'})

    pushed = slots.sync(context(gh, runner=pulumi), only='zerotier-network')

    # Not a credential, but a workflow input that has nowhere else to come from
    # and can only be passed as a secret.
    assert len(pushed) == len(slots.ZEROTIER_PHYSICAL) + len(slots.ZEROTIER_DNS)
    assert gh.values[(REPOSITORY, 'dns', 'ZEROTIER_NETWORK_ID')] == 'a-network'


def test_a_minted_row_refuses_by_naming_the_command_that_delivers_it() -> None:
    # A minted credential is disclosed once, to the call that made it, so this
    # map can only say where it goes and who puts it there.
    row = slots.ROWS['oci-physical']

    with pytest.raises(SlotRefused, match='credentials derived oci-physical mint'):
        _ = row.source.value(context(RecordedGh()))


def test_a_row_with_no_github_slot_is_skipped_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    gh = RecordedGh()

    pushed = slots.sync(context(gh, open_vault=opened), rows={'talos': slots.ROWS['talos']})

    assert pushed == []
    assert slots.ROWS['talos'].pending in caplog.text


def test_a_row_that_cannot_produce_its_value_does_not_stop_the_walk(caplog: pytest.LogCaptureFixture) -> None:
    gh = RecordedGh()
    both = {name: slots.ROWS[name] for name in ('zerotier-identity-dns', 'pulumi-passphrase')}

    # Most of the map is waiting on something, so a whole-map run has to deliver
    # what it can and report the rest — and still exit non-zero, because the map
    # is not filled.
    with pytest.raises(SlotRefused, match='zerotier-identity-dns'):
        _ = slots.sync(context(gh, open_vault=opened), rows=both)

    assert gh.values[(REPOSITORY, 'dns', 'PULUMI_CONFIG_PASSPHRASE')] == PASSPHRASE
    assert 'has no state' in caplog.text


def test_naming_a_row_with_no_github_slot_is_refused_rather_than_ignored() -> None:
    # A request for one specific row that quietly does nothing is worse than a
    # refusal: the operator walks away believing the slot is filled.
    with pytest.raises(SlotRefused, match='no GitHub secret slot'):
        _ = slots.sync(context(RecordedGh()), only='talos')


def test_an_unknown_row_name_is_refused_before_anything_is_pushed() -> None:
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='no slot map row named'):
        _ = slots.sync(context(gh), only='nonesuch')

    assert gh.invocations == []


def test_the_listing_prints_every_row_with_its_source_and_its_slots() -> None:
    printed = '\n'.join(slots.describe())

    for name, row in slots.ROWS.items():
        assert f'{name} ({row.source.kind})' in printed
        for target in row.targets:
            assert str(target) in printed
