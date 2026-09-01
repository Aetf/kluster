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

import functools
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fake_gh import RecordedGh
from test_cli import commands as cli_commands

from kluster import conventions
from kluster.scripts.credentials import devices, escrow, pki, pulumi_config, slots
from kluster.scripts.credentials.github_secrets import Forge, Slot
from kluster.scripts.credentials.pulumi_config import SlotRefused

REPOSITORY = slots.REPOSITORY
PASSPHRASE = 'a-recovered-passphrase'

#: The appliance this workstation's own bundle points at, which is where the
#: `ci` certificate's address comes from. A documentation range, so nothing
#: here can be mistaken for the real box.
APPLIANCE = '198.51.100.7'
OPERATOR_URL = f'postgres://operator@{APPLIANCE}:5432/pulumi_state?sslmode=verify-full'


def _command_line(argv: list[str]) -> str:
    """One leaf of the command tree as a `credentials …` line the map may name.

    The walk that produces these fills in whatever a parser insists on, so the
    options and their placeholder values are dropped: what a map row names is
    the subcommand, never a particular invocation of it.
    """
    words = [word for word in argv if not word.startswith('-') and not word.startswith('placeholder')]
    return ' '.join(('credentials', *words))


#: §3 rows the map deliberately does not carry. Empty, and meant to stay that
#: way: a credential the register names is a credential something has to
#: deliver, so a new row belongs in the map with its `pending` reason rather
#: than in an exception list. An entry here needs a sentence saying why the row
#: can never have one.
UNMAPPED: frozenset[str] = frozenset()


def register_table() -> dict[str, str]:
    """§3's table in the document's own words: each credential, and its Slot cell.

    Read out of the file rather than copied here: a copy is a third description
    of the inventory, and the point of this test is that there are two.
    """
    document = (pulumi_config.project_dir() / 'docs' / 'credentials.md').read_text()
    section = document.split('\n## 3. ', 1)[1].split('\n## 4. ', 1)[0]
    rows = [line.split('|') for line in section.splitlines() if line.startswith('| ')]
    for row in rows:
        assert len(row) == 7, f'§3 has five columns; this line has {len(row) - 2}: {"|".join(row)[:60]}'
    return {
        row[1].strip(): row[4].strip()
        for row in rows
        if row[1].strip() != 'Credential' and not row[1].strip().startswith('---')
    }


def register_credentials() -> list[str]:
    """The first column alone, which is what the two name-equality tests compare."""
    return list(register_table())


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


def test_a_row_the_map_calls_built_is_a_command_the_tree_carries() -> None:
    # `derived ls` prints each minted row's producer, with an "unbuilt" note
    # where there is none. An operator reads that as an instruction, so a row
    # naming a `credentials derived` command it does not carry -- or carrying
    # one while still calling itself unbuilt -- sends them to a subcommand that
    # is not there, or away from one that is.
    tree = {_command_line(argv) for argv in cli_commands()}

    for name, row in slots.ROWS.items():
        source = row.source
        if not isinstance(source, slots.Minted) or not source.command.startswith('credentials derived '):
            continue
        assert (source.command in tree) == (not source.unbuilt), (
            f'the map says {name} is {"unbuilt" if source.unbuilt else "built"}, and '
            f'`{source.command}` {"is" if source.command in tree else "is not"} in the command tree'
        )


def declared_environments() -> dict[str, set[str]]:
    """Every Environment the forge stack declares, by the repository it declares it in."""
    # Imported here rather than at module scope: `slots` must not depend on the
    # Pulumi provider SDKs, and this is the file that ties the two together
    # without letting the dependency into the command.
    from kluster.stacks import github

    return {
        f'{github.OWNER}/{github.DEPLOYMENT_REPO}': {
            *github.PREVIEWED_LAYERS,
            github.PLAN_ENVIRONMENT,
            github.APPLY_ENVIRONMENT,
        },
        f'{github.OWNER}/{github.OPS_REPO}': {github.DRILL_ENVIRONMENT},
    }


def undeclared_environments(rows: Mapping[str, slots.Row]) -> dict[str, set[str]]:
    """The sinks' Environments the forge stack does not declare **in their own repository**.

    Partitioned by repository rather than pooled: the two repositories have
    disjoint Environments, so a pooled comparison passes a `drill` secret aimed
    at the deployment repository and fails a correct one aimed at the ops
    repository.
    """
    declared = declared_environments()
    undeclared: dict[str, set[str]] = {}
    for row in rows.values():
        for slot in row.sinks:
            if slot.environment is None or slot.environment in declared.get(slot.repository, set()):
                continue
            undeclared.setdefault(slot.repository, set()).add(slot.environment)
    return undeclared


def test_the_map_targets_environments_the_forge_stack_declares() -> None:
    from kluster.stacks import github

    assert slots.REPOSITORY == f'{github.OWNER}/{github.DEPLOYMENT_REPO}'
    assert slots.OPS_REPOSITORY == f'{github.OWNER}/{github.OPS_REPO}'
    assert set(slots.ENVIRONMENTS) == declared_environments()[slots.REPOSITORY]
    # The ops repository's one Environment, held by name the way the deployment
    # repository's are: the map addresses it as a constant, and the stack that
    # creates it is the only thing that decides what it is called.
    assert slots.DRILL_ENVIRONMENT == github.DRILL_ENVIRONMENT
    assert declared_environments()[slots.OPS_REPOSITORY] == {slots.DRILL_ENVIRONMENT}
    # A secret pushed into an Environment the stack does not declare is a
    # secret no job will ever see.
    assert undeclared_environments(slots.ROWS) == {}


def sinking_into(slot: Slot) -> Mapping[str, slots.Row]:
    """A one-row map whose only delivery is that slot."""
    return {'a-row': slots.Row(register='a credential', source=slots.Derived('a-label'), targets=(slot,))}


def test_an_environment_is_held_against_the_repository_its_sink_names() -> None:
    drill = Slot(repository=slots.OPS_REPOSITORY, name='A_SECRET', environment=slots.DRILL_ENVIRONMENT)
    misplaced = Slot(repository=slots.REPOSITORY, name='A_SECRET', environment=slots.DRILL_ENVIRONMENT)
    foreign = Slot(repository=slots.OPS_REPOSITORY, name='A_SECRET', environment=slots.ENVIRONMENTS[0])

    # The drills' credentials live in the ops repository's own Environment, so
    # the first sink to name it must pass rather than read as undeclared.
    assert undeclared_environments(sinking_into(drill)) == {}
    # And each of the other two is a secret no job can see: the Environment is
    # real, but not in the repository the sink names.
    assert undeclared_environments(sinking_into(misplaced)) == {slots.REPOSITORY: {slots.DRILL_ENVIRONMENT}}
    assert undeclared_environments(sinking_into(foreign)) == {slots.OPS_REPOSITORY: {slots.ENVIRONMENTS[0]}}


#: How §3's Slot column separates the channels one credential lands in.
CHANNEL_SEPARATOR = '·'

#: The word in an entry's qualifier that marks a channel the register promises
#: and nothing addresses yet -- the cell's half of a map row's `pending`.
PENDING = 'pending'


def promised_channels(cell: str) -> dict[str, bool]:
    """A §3 Slot cell read as the map's vocabulary: each channel, and whether it is pending."""
    promised: dict[str, bool] = {}
    for entry in cell.split(CHANNEL_SEPARATOR):
        text = entry.strip()
        terms = [term for term in slots.REGISTER_COLUMNS if text.startswith(term)]
        assert len(terms) == 1, f'{text!r} begins with no channel term: {sorted(slots.REGISTER_COLUMNS)}'
        assert terms[0] not in promised, f'{cell!r} names {terms[0]!r} twice'
        promised[terms[0]] = PENDING in text
    return promised


def drifted_channels(cell: str, rows: Sequence[slots.Row]) -> tuple[set[str], set[str]]:
    """Where a §3 Slot cell and the rows implementing that credential disagree.

    Two ways to disagree, and the pair separates them: a channel the cell hands
    out as delivered that no row addresses, and a channel a row addresses that
    the cell does not name. A cell may still name a channel that has no
    address, which is what `pending` says.
    """
    promised = promised_channels(cell)
    delivered = {term for term, waiting in promised.items() if not waiting}
    addressed = {slots.register_column(target) for row in rows for target in row.targets}
    return delivered - addressed, addressed - promised.keys()


def test_every_register_slot_cell_names_the_channels_its_rows_address() -> None:
    # §3's first column is held against the map by name above; this is the same
    # equality one column over, which is what keeps a cell from promising a
    # delivery no row makes. More than one row may implement one credential, so
    # the cell is held against all of their channels together.
    for credential, cell in register_table().items():
        rows = [row for row in slots.ROWS.values() if row.register == credential]

        assert drifted_channels(cell, rows) == (set(), set()), credential
        # A channel the register promises without an address is what a row's
        # `pending` is for, so the cell may say `pending` only where a row says
        # what stands in the way.
        if any(waiting for waiting in promised_channels(cell).values()):
            assert [row for row in rows if row.pending], credential


def test_a_cell_promising_a_channel_no_row_delivers_is_drift() -> None:
    # The drift this vocabulary exists to catch. A cell naming a CI Environment
    # secret beside a state entry reads, to anyone working from the register, as
    # a credential CI holds -- while nothing pushes one and no slot exists to
    # push it into.
    stated = [
        slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            targets=(slots.PulumiState('a-stack', 'a value'),),
        )
    ]

    assert drifted_channels('Pulumi state · CI env', stated) == ({'CI env'}, set())
    # Saying it is pending is how the register promises a channel honestly: the
    # row then has to carry the reason.
    assert drifted_channels('Pulumi state · CI env (pending)', stated) == (set(), set())
    # And the other direction: a slot the map fills that the register does not
    # mention is a delivery its reader cannot know about.
    assert drifted_channels('CI env (pending)', stated) == (set(), {'Pulumi state'})


def test_the_passphrase_reaches_every_environment() -> None:
    # Every job runs a `pulumi` command, and both Pulumi channels are encrypted
    # under this one value, so a missing Environment here is a layer of the
    # merge chain that cannot start.
    passphrase = slots.ROWS['pulumi-passphrase']

    assert {slot.environment for slot in passphrase.sinks} == set(slots.ENVIRONMENTS)
    assert {slot.name for slot in passphrase.sinks} == {'PULUMI_CONFIG_PASSPHRASE'}


def workflow_backend_secrets() -> set[str]:
    """Every `PULUMI_BACKEND_*` secret the workflows in this repository read.

    Read out of the workflows for the reason §3 is read out of the document: the
    map exists to fill what CI names, and a name only one of the two knows is a
    job that starts with an empty file where a certificate should be.
    """
    workflows = pulumi_config.project_dir() / '.github' / 'workflows'
    text = '\n'.join(path.read_text() for path in sorted(workflows.glob('*.yml')))
    return set(re.findall(r'secrets\.(PULUMI_BACKEND_[A-Z]+)', text))


def test_the_client_bundle_fills_every_carrier_the_workflows_read() -> None:
    bundle = slots.ROWS['state-backend-certificates']

    # The connection string and the three files it authenticates with. Spelled
    # out so that a regex which stopped matching cannot make the comparison
    # below pass by leaving both sides empty.
    carriers = workflow_backend_secrets()
    assert len(carriers) == 4
    # They are file contents rather than a job's environment: the composite
    # action writes each into the checkout's slot, from which `mise.toml`
    # resolves the URL and the three `PGSSL*` variables.
    assert {slot.name for slot in bundle.sinks} == carriers
    # Every Environment, for the same reason the passphrase reaches every
    # Environment: each one runs a `pulumi` command against the backend.
    assert {slot.environment for slot in bundle.sinks} == set(slots.ENVIRONMENTS)
    assert not bundle.pending


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


@pytest.mark.parametrize(
    ('name', 'label'),
    [('github-dispatch-key', escrow.DISPATCH_KEY), ('github-trigger-key', escrow.TRIGGER_KEY)],
)
def test_an_app_key_is_recovered_from_its_escrow_and_says_what_it_still_waits_on(name: str, label: str) -> None:
    row = slots.ROWS[name]

    # Recovered rather than typed in again, which is the whole point of
    # escrowing a value made in a console: filling a slot later costs a
    # command instead of another visit to the page that generates the key.
    assert row.source == slots.Derived(label)
    # And no other slot yet: the workflow that reads it is not built, so the
    # row says so instead of naming a secret a future workflow would have to
    # guess right.
    assert row.targets == (slots.EscrowCopy(label),)
    assert not row.sinks
    assert row.pending


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


@functools.cache
def ca_pem() -> str:
    """One certificate authority for the whole file; generating a key is the slow part."""
    return pki.generate_ca_key()


class Vault(escrow.Vault):
    """An escrow that recovers without a key, standing in for the kit's own."""

    def recover(self, label: str, generation: int | None = None) -> str:
        assert label in escrow.register() or label.startswith(escrow.BACKUP), label
        # The CA has to be a real key: the row that recovers it issues a
        # certificate under it rather than pushing it anywhere.
        return ca_pem() if label == escrow.CA else PASSPHRASE


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
    backend_url: str | None = None,
) -> slots.Context:
    """A push that reaches nothing real: no kit, no backend, no forge, no terminal."""
    resolved = pulumi_config.BackendEnvironment(url=backend_url)
    return slots.Context(
        forge=Forge(token='the-account-root', run=gh),
        open_vault=open_vault,
        open_environment=lambda: resolved,
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


def pushed_bundle(gh: RecordedGh, environment: str = 'dns') -> dict[str, str]:
    """The four carriers as they landed in one Environment."""
    return {name: gh.values[(REPOSITORY, environment, name)] for name in workflow_backend_secrets()}


def test_the_client_bundle_is_issued_once_and_split_across_its_carriers() -> None:
    gh = RecordedGh()

    pushed = slots.sync(
        context(gh, open_vault=opened, backend_url=OPERATOR_URL),
        only='state-backend-certificates',
    )

    assert len(pushed) == 4 * len(slots.ENVIRONMENTS)
    carriers = pushed_bundle(gh)
    # The certificate and the key that opens it come out of a single issuance,
    # so the two halves have to be a pair. Resolving the row twice would put
    # halves of two bundles into one Environment, and the box would refuse the
    # handshake with nothing to say about why.
    certificate = x509.load_pem_x509_certificate(carriers[slots.BACKEND_CERT].encode())
    key = serialization.load_pem_private_key(carriers[slots.BACKEND_KEY].encode(), password=None)
    assert certificate.public_key() == key.public_key()
    # The Common Name is the Postgres role the box maps the certificate to.
    assert certificate.subject.rfc4514_string() == 'CN=ci'
    # No trailing newline on any of them: a secret is stored exactly as it is
    # piped in, and the action writes it out with a `printf` that appends one.
    # A carrier that ends in a newline reaches the runner's slot as a file with
    # two, which is not what a workstation's own bundle looks like.
    assert not [name for name, carried in carriers.items() if carried != carried.strip()]


def test_the_carried_connection_string_names_the_box_and_no_path() -> None:
    gh = RecordedGh()

    _ = slots.sync(
        context(gh, open_vault=opened, backend_url=OPERATOR_URL),
        only='state-backend-certificates',
    )

    url = pushed_bundle(gh)[slots.BACKEND_URL]
    # The `ci` role, the address this workstation's own bundle names, and no
    # file path: the three certificates are named by the `PGSSL*` variables
    # `mise.toml` derives from where the runner wrote them, which is what lets
    # one string serve every machine.
    assert url.startswith(f'postgres://ci@{APPLIANCE}:')
    assert 'sslcert=' not in url and 'sslrootcert=' not in url and 'sslkey=' not in url


def test_every_environment_receives_the_same_bundle() -> None:
    gh = RecordedGh()

    _ = slots.sync(
        context(gh, open_vault=opened, backend_url=OPERATOR_URL),
        only='state-backend-certificates',
    )

    # One issuance fanned out, not one per Environment: five certificates would
    # be five things to reason about the day a handshake is refused.
    for environment in slots.ENVIRONMENTS:
        assert pushed_bundle(gh, environment) == pushed_bundle(gh)


def test_pushing_the_bundle_again_issues_a_certificate_rather_than_re_reading_one() -> None:
    gh = RecordedGh()
    pushing = context(gh, open_vault=opened, backend_url=OPERATOR_URL)

    _ = slots.sync(pushing, only='state-backend-certificates')
    first = pushed_bundle(gh)
    _ = slots.sync(pushing, only='state-backend-certificates')
    second = pushed_bundle(gh)

    # The leaf key is random at issuance and escrowed nowhere, so a re-push is
    # a new credential rather than a copy of the one CI holds. Nothing is
    # retired by it: the CA does not revoke, and it authenticates the CA rather
    # than a particular leaf, so the predecessor works until it expires.
    assert second[slots.BACKEND_CERT] != first[slots.BACKEND_CERT]
    assert second[slots.BACKEND_KEY] != first[slots.BACKEND_KEY]
    # The CA certificate is re-signed with the same escrowed key, so the chain
    # a running job already trusts still verifies.
    authorities = [x509.load_pem_x509_certificate(run[slots.BACKEND_CA].encode()) for run in (first, second)]
    assert authorities[0].public_key() == authorities[1].public_key()


def test_the_bundle_cannot_be_issued_on_a_workstation_that_has_none_itself() -> None:
    gh = RecordedGh()

    # Which box to issue for is read off this workstation's own bundle, so a
    # machine with no bundle is told to write one rather than asked to type an
    # address that could name the wrong appliance.
    with pytest.raises(SlotRefused, match='no client bundle'):
        _ = slots.sync(context(gh, open_vault=opened), only='state-backend-certificates')

    assert not gh.values


def test_a_backend_url_that_names_no_host_is_refused() -> None:
    gh = RecordedGh()
    broken = context(gh, open_vault=opened, backend_url='postgres:///pulumi_state')

    with pytest.raises(SlotRefused, match='names no host'):
        _ = slots.sync(broken, only='state-backend-certificates')


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


def test_a_state_read_of_an_output_that_is_not_a_string_says_so() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'], outputs={'ci_zerotier_identity_dns': None})

    # A `null` output would otherwise be coerced into the four characters
    # `null` and pushed as though CI had been handed an identity.
    with pytest.raises(SlotRefused, match='must be a non-empty string'):
        _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')


def test_the_network_id_is_pushed_from_the_constant_that_decides_it() -> None:
    gh = RecordedGh()

    pushed = slots.sync(context(gh), only='zerotier-network')

    # Not a credential, but a workflow input that has nowhere else to come from
    # and can only be passed as a secret. Its value is the adopted network's
    # identity, which is a constant rather than a configured one, so this row
    # opens no stack at all.
    assert len(pushed) == len(slots.ZEROTIER_PHYSICAL) + len(slots.ZEROTIER_DNS)
    assert gh.values[(REPOSITORY, 'dns', 'ZEROTIER_NETWORK_ID')] == conventions.overlay.NETWORK_ID


def test_a_minted_row_refuses_by_naming_the_command_that_delivers_it() -> None:
    # A minted credential is disclosed once, to the call that made it, so this
    # map can only say where it goes and who puts it there.
    row = slots.ROWS['oci-physical']

    with pytest.raises(SlotRefused, match='credentials derived oci-physical mint'):
        _ = row.resolve(context(RecordedGh()))


def test_a_minted_row_is_not_this_command_s_business(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    gh = RecordedGh()
    minted = {'oci-physical': slots.ROWS['oci-physical']}

    pushed = slots.sync(context(gh), rows=minted)

    # A minted credential is born into its slot, so a walk that reported it as
    # something still to do would be reporting on a row that is already
    # delivered. It is passed over without a word, and nothing is read.
    assert pushed == []
    assert gh.invocations == []
    assert 'oci-physical' not in caplog.text


def test_naming_a_minted_row_is_refused_by_pointing_at_the_command_that_mints_it() -> None:
    # Obtaining the value again means minting again, which rotates a live
    # credential; the operator asking for a copy has to hear that rather than
    # watch a run do nothing.
    with pytest.raises(SlotRefused, match='born into its slot'):
        _ = slots.sync(context(RecordedGh()), only='oci-physical')


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


def test_a_sink_that_has_been_retired_answers_with_where_the_fact_lives_now() -> None:
    gh = RecordedGh()

    # Someone working from a runbook older than the move gets the new home
    # rather than "no such row", which is true and tells them nothing.
    with pytest.raises(SlotRefused, match=r'conventions\.OCI_TENANCY\.tenancy_ocid'):
        _ = slots.sync(context(gh), only='ociTenancyOcid')

    assert gh.invocations == []


def test_the_installation_token_row_answers_with_the_key_it_would_be_minted_from() -> None:
    gh = RecordedGh()

    # An installation token is working material of one workflow run and is
    # stored nowhere, so it is no row of this map. Someone reading a document
    # written while it was one gets pointed at the keys instead of at "no such
    # row", which is true and useless.
    with pytest.raises(SlotRefused, match='github-dispatch-key'):
        _ = slots.sync(context(gh), only='github-installation-tokens')

    assert gh.invocations == []


def test_no_row_still_delivers_a_fact_a_convention_now_owns() -> None:
    # The other half of a retirement: the sink is gone from the map, not merely
    # documented as gone, so nothing pushes a second copy of the fact.
    addressed = {
        target.key for row in slots.ROWS.values() for target in row.targets if isinstance(target, slots.PulumiConfig)
    }

    assert not addressed & set(slots.RETIRED)


def test_the_listing_prints_every_row_with_its_source_and_its_slots() -> None:
    printed = '\n'.join(slots.describe())

    for name, row in slots.ROWS.items():
        assert f'{name} ({row.source.kind})' in printed
        for target in row.targets:
            assert str(target) in printed
