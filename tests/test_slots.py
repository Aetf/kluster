"""The slot map, held against the register it mirrors, and the push that reads it.

Two halves. The first reads `docs/credentials.md` — §3's table, and the closed
set of channels §1 rule 6 states — and compares it with the map: a credential
in the table with no map row, a map row naming a credential the table does not,
a cell whose secret names are not the ones its rows push under (a name no row
pushes, or one a row pushes that a cell naming the others leaves out), or a
channel the map has a term for that rule 6 does not name, fails here — which is
the only thing keeping two descriptions of one inventory from drifting apart.
The second drives `slots.sync` against a `gh` that runs nothing, because what
is under test is which slots a row fills and what it says when it cannot fill
one; the subprocess itself is `test_github_secrets.py`.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, get_args

import pytest
import yaml
from credentials_command_tree import commands as cli_commands
from credentials_command_tree import named_commands, quoted_commands
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from fake_gh import RecordedGh

from kluster import conventions
from kluster.lib import pulumi_cli, stack_environment
from kluster.lib.state_backend import settings as appliance_settings
from kluster.scripts.credentials import devices, escrow, pki, pulumi_config, slots
from kluster.scripts.credentials.github_secrets import Forge, Slot
from kluster.scripts.credentials.pulumi_config import SlotRefused

#: The forge census, read here as the map reads it (`conventions.forge`).
#: Copying the repository names or ci.md §3's Environments into this file
#: would make it one more description of the partition, and a test that holds
#: the map against a copy says nothing about the census.
REPOSITORY = conventions.forge.DEPLOYMENT.full_name
OPS_REPOSITORY = conventions.forge.OPS.full_name
DRILL_ENVIRONMENT = conventions.forge.DRILL.name
ENVIRONMENTS = tuple(environment.name for environment in conventions.forge.DEPLOYMENT.environments)
#: The Environments a job of a pull request or of a branch push can never
#: enter: those that take protected branches only, read off the census.
MAIN_ONLY = frozenset(
    environment.name
    for environment in conventions.forge.DEPLOYMENT.environments
    if environment.branches is conventions.forge.BranchPolicy.PROTECTED_ONLY
)
#: The Environments the stack passphrase is pushed into, every one but those
#: holding `physical`'s own, as the map partitions them.
STACK_PASSPHRASE_ENVIRONMENTS = tuple(name for name in ENVIRONMENTS if name not in slots.PHYSICAL_ENVIRONMENTS)
#: The output the `dns` identity's row reads, as the `physical` program exports
#: it: the fake stack below answers under this name for the same reason the
#: names above are read rather than copied.
CI_DNS_IDENTITY = conventions.PHYSICAL_OUTPUTS.ci_identity['ci-dns']

PASSPHRASE = 'a-recovered-passphrase'

#: The passphrases a state read and a config write are started under: the
#: `physical` stack's own, and the one every other stack shares.
PHYSICAL_PASSPHRASE = 'the-physical-passphrase'
STACK_PASSPHRASE = 'the-stack-passphrase'

PHYSICAL_STACK = slots.PHYSICAL_STACK
DNS_STACK = slots.DNS_STACK

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


def test_a_command_a_row_names_is_one_the_tree_carries() -> None:
    # `derived ls` prints each row's source (`describe()`), which names the
    # commands that produce or deliver it, with an "unbuilt" note where a minted
    # row's producer does not exist yet. An operator reads that as an
    # instruction, so a row naming a `credentials` command the tree does not
    # carry -- or carrying one while still calling itself unbuilt -- sends them
    # to a subcommand that is not there, or away from one that is.
    #
    # Two reads, so that neither a misspelling nor a new kind of source slips
    # out of scope. A source's producer (its `command`) is either a
    # `credentials` line or a program described as "the …" -- a producer that
    # is neither is a line misspelt at its first word, and fails rather than
    # being taken for a program. And every `credentials` line `describe()`
    # prints is held to the tree too, which reaches a command a source names
    # outside its producer, or under a field named otherwise.
    tree = {_command_line(argv) for argv in cli_commands()}
    producers = {name: str(getattr(row.source, 'command', '')) for name, row in slots.ROWS.items()}
    assert any(producer.startswith('credentials ') for producer in producers.values()), (
        'nothing to check: no map row names a `credentials` command as its producer'
    )

    named: list[tuple[str, str, bool]] = []
    for name, row in slots.ROWS.items():
        producer = producers[name]
        assert not producer or producer.startswith(('credentials ', 'the ')), (
            f'{name} names {producer!r} as its producer, which is neither a `credentials` command nor a program'
        )
        unbuilt = bool(getattr(row.source, 'unbuilt', ''))
        commands = {' '.join(('credentials', *argv)) for argv in quoted_commands(row.source.describe())}
        if producer.startswith('credentials '):
            commands.add(producer)
        named += [(name, command, unbuilt and command == producer) for command in sorted(commands)]

    for name, command, unbuilt in named:
        assert (command in tree) == (not unbuilt), (
            f'the map says {name} is {"unbuilt" if unbuilt else "built"}, and '
            f'`{command}` {"is" if command in tree else "is not"} in the command tree'
        )


def declared_environments() -> dict[str, set[str]]:
    """Every Environment the forge census declares, by the repository it sits in."""
    return {
        repository.full_name: {environment.name for environment in repository.environments}
        for repository in conventions.forge.REPOSITORIES
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


def test_the_map_targets_environments_the_forge_census_declares() -> None:
    """A secret pushed into an Environment nothing declares is one no job will ever see.

    The map and the `github` stack read the same census, so what is left to
    check is the rows: a row may name any Environment it likes -- several do,
    literally (`ZEROTIER_PHYSICAL`, `ZEROTIER_DNS`) -- and only the census says
    which of them exist.
    """
    assert undeclared_environments(slots.ROWS) == {}


def sinking_into(slot: Slot) -> Mapping[str, slots.Row]:
    """A one-row map whose only delivery is that slot."""
    return {'a-row': slots.Row(register='a credential', source=slots.Derived('a-label'), targets=(slot,))}


def test_an_environment_is_held_against_the_repository_its_sink_names() -> None:
    drill = Slot(repository=OPS_REPOSITORY, name='A_SECRET', environment=DRILL_ENVIRONMENT)
    misplaced = Slot(repository=REPOSITORY, name='A_SECRET', environment=DRILL_ENVIRONMENT)
    foreign = Slot(repository=OPS_REPOSITORY, name='A_SECRET', environment=ENVIRONMENTS[0])

    # The drills' credentials live in the ops repository's own Environment, so
    # the first sink to name it must pass rather than read as undeclared.
    assert undeclared_environments(sinking_into(drill)) == {}
    # And each of the other two is a secret no job can see: the Environment is
    # real, but not in the repository the sink names.
    assert undeclared_environments(sinking_into(misplaced)) == {REPOSITORY: {DRILL_ENVIRONMENT}}
    assert undeclared_environments(sinking_into(foreign)) == {OPS_REPOSITORY: {ENVIRONMENTS[0]}}


#: How §3's Slot column separates the channels one credential lands in.
CHANNEL_SEPARATOR = '·'

#: The word in an entry's qualifier that marks a channel the register promises
#: and nothing addresses yet -- the cell's half of a map row's `pending`. Whole
#: word, so that a qualifier reading "depending on" is not a promise deferred.
PENDING = re.compile(r'\bpending\b')


def channel_entries(cell: str) -> dict[str, str]:
    """A §3 Slot cell split into its entries, each under the channel term it begins with."""
    entries: dict[str, str] = {}
    for entry in cell.split(CHANNEL_SEPARATOR):
        text = entry.strip()
        # The term has to end where a word ends: `escrowed copy` begins with the
        # letters of `escrow` and is not that channel.
        terms = [term for term in slots.REGISTER_COLUMNS if re.match(f'{re.escape(term)}\\b', text)]
        assert len(terms) == 1, f'{text!r} begins with no channel term: {sorted(slots.REGISTER_COLUMNS)}'
        assert terms[0] not in entries, f'{cell!r} names {terms[0]!r} twice'
        entries[terms[0]] = text
    return entries


def promised_channels(cell: str) -> dict[str, bool]:
    """A §3 Slot cell read as the map's vocabulary: each channel, and whether it is pending."""
    return {term: PENDING.search(text) is not None for term, text in channel_entries(cell).items()}


def channels_of_every_kind() -> tuple[slots.Channel, ...]:
    """One channel of each kind the map may deliver into, `Slot` in all four of its guises."""
    return (
        Slot(repository=REPOSITORY, name='A_SECRET'),
        Slot(repository=REPOSITORY, name='A_SECRET', environment=ENVIRONMENTS[0]),
        Slot(repository=OPS_REPOSITORY, name='A_SECRET'),
        Slot(repository=OPS_REPOSITORY, name='A_SECRET', environment=DRILL_ENVIRONMENT),
        slots.PulumiConfig('a-stack', 'aKey'),
        slots.PulumiState('a-stack', 'a value'),
        slots.EscrowCopy('a-label'),
        slots.SealedSecret(conventions.sealed.DNS01_TOKEN),
        slots.OnBox('a file'),
        slots.SecretStore('a-key'),
        slots.WorkstationSlot('a-file'),
        slots.DeviceSecret('a value'),
    )


def test_the_vocabulary_names_every_channel_of_the_closed_set() -> None:
    channels = channels_of_every_kind()

    # Rule 6's set of channels is closed, and §3 has to be able to say every
    # member of it: one added to the union with no term of its own is a delivery
    # the register can only describe in words nothing checks.
    assert {type(channel) for channel in channels} == set(get_args(slots.Channel))
    assert {slots.register_column(channel) for channel in channels} == slots.REGISTER_COLUMNS


def rule_6_channels() -> set[str]:
    """§1 rule 6's closed set in the document's own words: the term each channel begins with.

    Read out of the file the way `register_table` reads §3, and in the grammar
    `promised_channels` reads a cell in -- `·`-separated, the fixed term first
    and a qualifier after it -- because the rule is where the closed set is
    stated in prose and the cells are where it is spelled per row, and the two
    are written by different hands.
    """
    document = (pulumi_config.project_dir() / 'docs' / 'credentials.md').read_text()
    rules = document.split('\n## 1. ', 1)[1].split('\n## 2. ', 1)[0]
    # The rule's first paragraph is the enumeration, opened by its bold lead
    # sentence; the paragraphs after the blank line describe members of it.
    numbered = rules.split('\n6.  ', 1)
    assert len(numbered) == 2, '§1 has no rule 6'
    item = ' '.join(numbered[1].split('\n\n', 1)[0].split())
    opened = re.fullmatch(r'\*\*[^*]+\*\*\s*(.+)', item)
    assert opened is not None, f'rule 6 does not open with a bold sentence: {item[:60]!r}'
    terms: set[str] = set()
    for entry in opened.group(1).split(CHANNEL_SEPARATOR):
        # A term is bold or plain, and ends where its parenthesized qualifier
        # opens or where the sentence after the last entry begins.
        found = re.match(r'[^(.]*', entry.replace('**', '').strip())
        term = found.group().strip() if found is not None else ''
        assert term, f'rule 6 has an entry with no term: {entry!r}'
        assert term not in terms, f'rule 6 names {term!r} twice'
        terms.add(term)
    return terms


def test_rule_6_names_every_channel_the_cells_have_a_term_for() -> None:
    named = rule_6_channels()

    # Rule 6 is the closed set as prose states it, and `_TERMS` is that set as
    # the cells spell it; a term renamed on one side alone is a channel the
    # register can then only describe in words nothing checks. A subset rather
    # than equality, because the rule names more than a cell can be delivered
    # into: the kit's own channel, which no §3 row lands in, and the GitHub
    # secrets under their prose names, which the cells abbreviate and
    # `promised_channels` holds against the cells alone.
    terms = {slots.register_column(channel) for channel in channels_of_every_kind() if not isinstance(channel, Slot)}

    assert terms <= named, sorted(terms - named)


def drifted_channels(cell: str, rows: Sequence[slots.Row]) -> tuple[set[str], set[str], set[str]]:
    """Where a §3 Slot cell and the rows implementing that credential disagree.

    Three ways to disagree, and the triple separates them: a channel the cell
    hands out as delivered that no row addresses, a channel a row addresses that
    the cell does not name, and a channel the two disagree about being
    `pending` -- a cell qualifying one no row is waiting on, a row waiting on
    one the cell hands out as delivered, or a channel one row fills while
    another says it is waiting.

    The third compares two sets of channels rather than counting reasons,
    because a row files each reason under the channel it is about: a cell
    deferring a CI Environment secret while the row is waiting on an
    ops-repository one is drift, though both sides have something pending. The
    filled-and-deferred half is kept beside that comparison rather than left to
    it, because a credential may be several rows -- a row's own `pending` is
    held against its own targets when it is built, and nothing holds it against
    a sibling's.
    """
    promised = promised_channels(cell)
    delivered = {term for term, waiting in promised.items() if not waiting}
    waiting = promised.keys() - delivered
    addressed = {slots.register_column(target) for row in rows for target in row.targets}
    unaddressed = {channel for row in rows for channel in row.pending}
    return delivered - addressed, addressed - promised.keys(), (waiting ^ unaddressed) | (waiting & addressed)


def test_every_register_slot_cell_names_the_channels_its_rows_address() -> None:
    # §3's first column is held against the map by name above; this is the same
    # equality one column over, which is what keeps a cell from promising a
    # delivery no row makes. More than one row may implement one credential, so
    # the cell is held against all of their channels together.
    for credential, cell in register_table().items():
        rows = [row for row in slots.ROWS.values() if row.register == credential]

        assert drifted_channels(cell, rows) == (set(), set(), set()), credential


#: A GitHub secret's name where a §3 Slot cell spells one out: a code span in
#: capitals and underscores, inside an entry whose channel is a GitHub secret.
#: Read from those entries alone, so a code span in capitals that qualifies
#: some other channel -- a file a workstation slot is read as, say -- is that
#: entry's business and not a secret name.
SECRET_NAME = re.compile(r'`([A-Z][A-Z0-9_]*)`')

#: The Slot column's terms for a GitHub secret, one per kind of `Slot`.
GITHUB_TERMS = frozenset(
    slots.register_column(channel) for channel in channels_of_every_kind() if isinstance(channel, Slot)
)


def test_a_secret_a_register_cell_names_is_the_one_its_rows_fill() -> None:
    """The names §3 gives a GitHub entry are the names the map pushes under that channel.

    The channel terms are held one case up, and a term says what kind of
    secret a credential lands in, not what the secret is called. The name is
    what a workflow author copies out of the register, so an entry that names
    its secrets is held to the `Slot.name`s, on that channel, of every row of
    that credential -- all of them, because an entry naming some of them reads
    as the whole list. An entry naming none ("CI env") claims no name to hold.
    """
    named = {
        (credential, term): set(SECRET_NAME.findall(text))
        for credential, cell in register_table().items()
        for term, text in channel_entries(cell).items()
        if term in GITHUB_TERMS
    }
    assert GITHUB_TERMS, 'nothing to check: no kind of `Slot` has a term'
    assert any(named.values()), 'nothing to check: no §3 GitHub entry names a secret'

    drifted: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
    for (credential, term), names in named.items():
        filled = {
            slot.name
            for row in slots.ROWS.values()
            if row.register == credential
            for slot in row.sinks
            if slots.register_column(slot) == term
        }
        if names and names != filled:
            drifted[(credential, term)] = (sorted(names), sorted(filled))

    assert drifted == {}


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
    waiting = [
        slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            targets=(slots.PulumiState('a-stack', 'a value'),),
            pending={'CI env': 'the workflow that would read it is not built'},
        )
    ]

    assert drifted_channels('Pulumi state · CI env', stated) == ({'CI env'}, set(), set())
    # Saying it is pending is how the register promises a channel honestly: a
    # row of that credential then has to say what *that* channel waits on.
    assert drifted_channels('Pulumi state · CI env (pending)', waiting) == (set(), set(), set())
    assert drifted_channels('Pulumi state · CI env (pending)', stated) == (set(), set(), {'CI env'})
    # The other direction: a slot the map fills that the register does not
    # mention is a delivery its reader cannot know about.
    assert drifted_channels('CI env (pending)', waiting) == (set(), {'Pulumi state'}, set())
    # And a filled channel the cell calls pending, which reads as work left to
    # do on a slot that already holds the credential.
    assert drifted_channels('Pulumi state (pending)', stated) == (set(), set(), {'Pulumi state'})


def test_a_reason_for_one_channel_does_not_excuse_a_pending_mark_on_another() -> None:
    # The looseness the keying removes. A cell may defer any channel it likes as
    # long as some row of that credential is waiting on something, and the two
    # need never be the same channel -- so a register saying "CI holds this,
    # once the workflow exists" reads as checked while the row is in fact
    # waiting on an ops-repository secret nobody asked about.
    elsewhere = [
        slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            targets=(slots.PulumiState('a-stack', 'a value'),),
            pending={'ops-repo secret': 'the ops-repository workflow that would read it is not built'},
        )
    ]

    assert drifted_channels('Pulumi state · CI env (pending)', elsewhere) == (
        set(),
        set(),
        {'CI env', 'ops-repo secret'},
    )


def test_a_channel_one_row_fills_while_another_defers_it_is_drift() -> None:
    # One credential may be several rows -- the ZeroTier CI identities are three
    # -- and a row's own reasons are held against its own targets when it is
    # built. Nothing holds them against a sibling's, so this is where a cell
    # that says `pending` at an operator already being served is caught.
    siblings = [
        slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            targets=(slots.PulumiState('a-stack', 'a value'),),
        ),
        slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            pending={'Pulumi state': 'the stack that would export it has never run'},
        ),
    ]

    assert drifted_channels('Pulumi state (pending)', siblings) == (set(), set(), {'Pulumi state'})


def test_a_sink_naming_a_repository_the_census_does_not_declare_is_refused() -> None:
    # The Slot column's terms are keyed by repository, so a sink in a repository
    # nothing declares has no term -- and a secret pushed there is one no job of
    # this installation can see. The map refuses it by name, at the row that
    # names it, rather than raising a lookup whose message is a tuple.
    with pytest.raises(SlotRefused, match='Aetf/not-a-repository'):
        _ = slots.register_column(Slot(repository='Aetf/not-a-repository', name='A_SECRET'))

    with pytest.raises(SlotRefused, match='Aetf/not-a-repository'):
        _ = slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            targets=(Slot(repository='Aetf/not-a-repository', name='A_SECRET'),),
        )


def test_a_reason_filed_under_a_channel_the_register_cannot_name_is_refused() -> None:
    # The keys are §3's own vocabulary, so a reason filed under anything else
    # explains a cell no reader can find -- and the interlock above would read
    # it as a channel the row waits on that the register never promised.
    with pytest.raises(SlotRefused, match='no channel of'):
        _ = slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            pending={'CI Environment secret': 'the workflow that would read it is not built'},
        )


def test_a_reason_filed_under_a_channel_the_row_addresses_is_refused() -> None:
    # A row cannot both name where a value lands and say that channel is
    # waiting on something: one of the two lines is wrong, and which one is a
    # question for whoever wrote them.
    with pytest.raises(SlotRefused, match='a channel this row addresses'):
        _ = slots.Row(
            register='a credential',
            source=slots.Derived('a-label'),
            targets=(slots.PulumiState('a-stack', 'a value'),),
            pending={'Pulumi state': 'the stack that would export it has never run'},
        )


def test_every_environment_holds_exactly_one_passphrase() -> None:
    """Each Environment's `PULUMI_CONFIG_PASSPHRASE` is filled by one passphrase row, never none and never two.

    Every job runs a `pulumi` command, so an Environment holding none is a
    layer of the merge chain that cannot start; one that two rows fill holds
    whichever the map's order pushed last.
    """
    passphrases = {
        name: {slot.environment for slot in slots.ROWS[name].sinks}
        for name in ('pulumi-passphrase', stack_environment.PHYSICAL_PASSPHRASE_ROW)
    }

    # One name in every Environment, the one every job reads its passphrase
    # from: which passphrase it gets is the Environment's to decide.
    assert workflow_secrets('PULUMI_CONFIG_') == {slots.PASSPHRASE_SECRET}
    for name in passphrases:
        assert {slot.name for slot in slots.ROWS[name].sinks} == {slots.PASSPHRASE_SECRET}
    for environment in ENVIRONMENTS:
        holders = [name for name, reached in passphrases.items() if environment in reached]
        assert len(holders) == 1, f'{environment} holds {holders or "no passphrase"}'


def test_physicals_passphrase_reaches_no_environment_a_pull_request_can_enter() -> None:
    """The property `physical`'s own passphrase exists for, held rather than merely true (rfc-005 §5.1).

    `physical`'s provider credentials are config secrets under it, so an
    Environment that takes any branch and holds it hands them to whatever a
    branch runs there, a preview or a workflow the branch adds. It goes to the
    Environments `physical`'s jobs run in, each of them one that takes
    protected branches only, and into no repository secret, which every job
    can read.
    """
    row = slots.ROWS[stack_environment.PHYSICAL_PASSPHRASE_ROW]
    reached = {slot.environment for slot in row.sinks}

    assert reached == set(slots.PHYSICAL_ENVIRONMENTS)
    assert reached <= MAIN_ONLY, f'{reached - MAIN_ONLY} take a branch a pull request can push'
    assert {slot.repository for slot in row.sinks} == {REPOSITORY}
    # The contrast, so this cannot pass by the census having no Environment
    # that takes any branch.
    assert set(ENVIRONMENTS) - MAIN_ONLY
    # And its escrow copy and its workstation slot, the names `recover` and
    # the driver-less run by hand use.
    assert slots.EscrowCopy(escrow.PHYSICAL_PASSPHRASE) in row.targets
    assert slots.WorkstationSlot(stack_environment.PHYSICAL_PASSPHRASE_SLOT) in row.targets
    written = escrow.slot(escrow.PHYSICAL_PASSPHRASE)
    assert written is not None
    assert written.store is None
    assert written.path().name == stack_environment.PHYSICAL_PASSPHRASE_SLOT
    assert escrow.row_name(escrow.PHYSICAL_PASSPHRASE) == stack_environment.PHYSICAL_PASSPHRASE_ROW


def workflow_secrets(prefix: str) -> set[str]:
    """Every secret whose name begins with `prefix` that the workflows in this repository read.

    Read out of the workflows for the reason §3 is read out of the document: the
    map exists to fill what CI names, and a name only one of the two knows is a
    job that starts with an empty value where a credential should be. The
    workflows alone, in both suffix spellings GitHub reads: a composite action
    has no `secrets` context, so nothing under `.github/actions/` can name one.
    """
    workflows = pulumi_config.project_dir() / '.github' / 'workflows'
    text = '\n'.join(path.read_text() for pattern in ('*.yml', '*.yaml') for path in sorted(workflows.glob(pattern)))
    return set(re.findall(rf'secrets\.({re.escape(prefix)}[A-Z0-9_]+)', text))


def workflow_backend_secrets() -> set[str]:
    """The carriers of a state-backend client bundle, as the workflows name them."""
    return workflow_secrets('PULUMI_BACKEND_')


def test_the_overlay_rows_fill_the_secrets_the_workflows_join_with() -> None:
    # The jobs that join ZeroTier hand the action an identity and a network id
    # under names the workflows spell and the map fills, and a name only one
    # side knows is a job that joins with an empty value. Held both ways on the
    # prefix, so a sink renamed out of it is as red as one renamed within it.
    read = workflow_secrets('ZEROTIER_')
    filled = {slot.name for row in slots.ROWS.values() for slot in row.sinks if slot.name.startswith('ZEROTIER_')}

    assert read, 'nothing to check: no workflow reads a `ZEROTIER_*` secret'
    assert filled == read


def test_the_client_bundle_fills_every_carrier_the_workflows_read() -> None:
    bundle = slots.ROWS['state-backend-certificates']

    # The connection string and the three files it authenticates with. Guarded
    # so that a regex which stopped matching cannot make the comparison below
    # pass by leaving both sides empty.
    carriers = workflow_backend_secrets()
    assert carriers, 'nothing to check: no workflow reads a `PULUMI_BACKEND_*` secret'
    # They are file contents rather than a job's environment: the composite
    # action writes each into the checkout's slot, from which `mise.toml`
    # resolves the URL and the three `PGSSL*` variables.
    assert {slot.name for slot in bundle.sinks} == carriers
    # Every Environment, for the same reason the passphrase reaches every
    # Environment: each one runs a `pulumi` command against the backend.
    assert {slot.environment for slot in bundle.sinks} == set(ENVIRONMENTS)
    assert not bundle.pending


#: `secure:` keys in a committed stack file that authenticate nothing, each with
#: the reason it is encrypted anyway. Every other `secure:` key is a credential,
#: and a credential is a slot-map row (§1 rule 3); an entry here is the one
#: way a key stays out of the map, and it needs a sentence saying why the value
#: opens nothing.
NOT_CREDENTIALS: Mapping[str, str] = {
    'budgetAlertRecipients': (
        'the addresses the cloud budget alerts go to -- personal mailboxes kept out of a public '
        'repository, not a value anything authenticates with'
    ),
}


def committed_secrets() -> dict[str, set[str]]:
    """Every `secure:` key in every committed `Pulumi.<stack>.yaml`, by stack, in the map's own key form.

    Read out of the files rather than listed, for the reason §3 is read out of
    the document: the point is that what the stack files hold and what the map
    delivers are two descriptions of one inventory. The project's own
    namespace is stripped because `pulumi config set` on a bare key adds it
    and `PulumiConfig.key` is bare; a `secure:` key in any other namespace is
    left whole, so it fails as unmapped rather than being mistaken for a bare
    one (rfc-002 §8.1 puts no credential of this repository in a provider's
    namespace).
    """
    project = pulumi_config.project_dir()
    prefix = f'{yaml.safe_load((project / "Pulumi.yaml").read_text())["name"]}:'
    found: dict[str, set[str]] = {}
    for path in sorted(project.glob('Pulumi.*.yaml')):
        stack = path.name.removeprefix('Pulumi.').removesuffix('.yaml')
        config = cast('dict[str, object]', yaml.safe_load(path.read_text()).get('config') or {})
        found[stack] = {
            key.removeprefix(prefix) for key, value in config.items() if isinstance(value, dict) and 'secure' in value
        }
    return found


def test_every_committed_config_secret_is_a_slot_map_target() -> None:
    committed = committed_secrets()
    # Spelled out so that a glob or a parse that stopped matching cannot make
    # the comparison below pass by finding nothing to compare.
    assert set(committed) >= {PHYSICAL_STACK, DNS_STACK}
    assert committed[PHYSICAL_STACK]

    delivered = {
        (target.stack, target.key)
        for row in slots.ROWS.values()
        for target in row.targets
        if isinstance(target, slots.PulumiConfig)
    }
    unmapped = sorted(
        f'{stack}: {key}'
        for stack, keys in committed.items()
        for key in keys
        if (stack, key) not in delivered and key not in NOT_CREDENTIALS
    )

    # A ciphertext in a committed stack file that no row delivers is a
    # credential the register cannot see: it reached the stack by some hand
    # `derived ls` does not print, and nothing says what rotates it. The
    # register interlock above cannot catch it, because a credential absent
    # from both the document and the map is invisible to a test holding the
    # two equal -- this is the third description, the one the stack reads.
    assert unmapped == []


def test_the_exemptions_are_committed_and_not_delivered() -> None:
    committed = {key for keys in committed_secrets().values() for key in keys}
    delivered = {
        target.key for row in slots.ROWS.values() for target in row.targets if isinstance(target, slots.PulumiConfig)
    }

    # An exemption names a key the files actually hold, or it is a sentence
    # about nothing; and one the map delivers as well is a credential wearing
    # a note that says it is not one.
    assert set(NOT_CREDENTIALS) <= committed
    assert set(NOT_CREDENTIALS).isdisjoint(delivered)


def test_no_device_field_is_delivered_into_the_committed_file_in_the_clear() -> None:
    # Rule 6's closed set names the Pulumi config secret and no plain committed
    # key, so the map has no way to say "this key is in the clear" -- and a
    # device field that was would be delivered under a term claiming the
    # opposite. A plain field therefore needs a channel in rule 6 and a term in
    # the vocabulary before it can have a row here, which is what this holds.
    #
    # The claim is about `devices.py`, and it is held here because it is the
    # map's vocabulary that cannot say it. `test_devices.py`'s
    # `test_every_delivered_field_takes_the_encrypted_channel` owns the sibling
    # claim about what the delivery does, so this moves there the day someone
    # owns both paths in one change.
    plain = {
        (member, field.name)
        for member, device in devices.DEVICES.items()
        for field in device.fields
        if not field.secret
    }

    assert plain == set()


def test_no_row_delivers_a_webhook_into_the_deployment_repository() -> None:
    """CI holds no Home Assistant credential; the ops repository's dispatch handler does.

    The secret `deploy.yml` still reads in the deployment repository is the
    legacy channel, which this map leaves alone rather than re-syncing, so no
    row of the map delivers a webhook there. Where the ops repository's copy
    lands is §3's cell, held by the cases above.
    """
    assert not [
        slot
        for row in slots.ROWS.values()
        for slot in row.sinks
        if slot.repository == REPOSITORY and 'WEBHOOK' in slot.name.upper()
    ]


def test_no_github_secret_is_filled_by_two_rows() -> None:
    # Two rows pushing one secret overwrite each other on every `derived sync`,
    # and which value survives is the order of the map.
    filled = Counter(slot for row in slots.ROWS.values() for slot in row.sinks)

    assert filled
    assert [slot for slot, count in filled.items() if count > 1] == []


def test_an_ops_repo_environment_secret_is_named_after_its_environment() -> None:
    """Inside a job an Environment secret shadows a repository secret of the same name.

    The ops repository is to hold both kinds (credentials.md §3), so every
    Environment secret there carries its Environment as a prefix -- which is
    what keeps a workflow naming the Environment from reading the drill's copy
    where it meant the repository's. Held over every such sink in the map, so
    a row added later is held to it without being named here.
    """
    environment_secrets = [
        (slot, slot.environment)
        for row in slots.ROWS.values()
        for slot in row.sinks
        if slot.repository == OPS_REPOSITORY and slot.environment is not None
    ]
    assert environment_secrets, 'nothing to check: no map row names an ops-repo Environment secret'

    for slot, environment in environment_secrets:
        assert slot.name.startswith(f'{environment.upper()}_'), slot


def test_an_ops_repo_repository_secret_is_named_after_no_environment() -> None:
    """The complement of the case above: the prefix is an Environment secret's alone.

    The prefix is what tells a workflow author which kind a secret is, so it
    says something only while no repository secret of the ops repository wears
    one. Held over every ops-repository secret with no Environment, against
    every Environment the census declares there.
    """
    prefixes = tuple(f'{environment.upper()}_' for environment in declared_environments()[OPS_REPOSITORY])
    repository_secrets = [
        slot
        for row in slots.ROWS.values()
        for slot in row.sinks
        if slot.repository == OPS_REPOSITORY and slot.environment is None
    ]
    assert prefixes, 'nothing to check: the census declares no Environment in the ops repository'
    assert repository_secrets, 'nothing to check: no map row names an ops-repo repository secret'

    assert [slot for slot in repository_secrets if slot.name.startswith(prefixes)] == []


# --------------------------------------------------------------------------
# The push.
# --------------------------------------------------------------------------


@dataclass
class RecordedPulumi:
    """Just enough `pulumi` for a state read and a config copy: which stacks exist, and what they hold.

    `outputs` are the state of the stack being read; `config` is keyed by stack
    and key, so a copy into two stacks is two entries. Each invocation is kept
    with the passphrase it was started under, which is how a case tells a read
    of `physical` from a write into a stack under the stack passphrase.
    """

    stacks: list[str] = field(default_factory=list[str])
    outputs: dict[str, object] = field(default_factory=dict[str, object])
    config: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    #: The keys stored in the clear rather than as secrets.
    plain: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    invocations: list[list[str]] = field(default_factory=list[list[str]])
    passphrases: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])

    def __call__(self, args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
        self.invocations.append(list(args))
        self.passphrases.append((' '.join(args), env.get(pulumi_config.PASSPHRASE_ENV)))
        match list(args):
            case ['stack', 'ls', '--json']:
                return json.dumps([{'name': name} for name in self.stacks])
            case ['stack', 'output', '--json', '--show-secrets', '--stack', _]:
                return json.dumps(self.outputs)
            case ['config', 'get', key, '--stack', stack]:
                if (stack, key) not in self.config:
                    # What the real CLI does with a key the stack has no value
                    # for: exit non-zero, which `run_pulumi` turns into this.
                    raise SlotRefused(f"`pulumi config get` failed: configuration key '{key}' not found")
                return self.config[(stack, key)] + '\n'
            case ['config', 'get', key, '--json', '--stack', stack]:
                if (stack, key) not in self.config:
                    raise SlotRefused(f"`pulumi config get` failed: configuration key '{key}' not found")
                # The shape the pinned CLI prints: the value, without the
                # newline the plain form adds, and whether it is encrypted.
                return json.dumps({'value': self.config[(stack, key)], 'secret': (stack, key) not in self.plain})
            case ['config', 'set', key, '--secret', '--stack', stack]:
                assert stdin is not None, 'a secret was passed as an argument rather than on standard input'
                # What the real CLI does with a value on standard input: it
                # drops one trailing newline.
                self.config[(stack, key)] = stdin.removesuffix('\n')
                self.plain.discard((stack, key))
                return ''
            case unknown:  # pragma: no cover - an invocation a read is not meant to make
                raise AssertionError(f'unexpected pulumi invocation {unknown}')


def unopened() -> escrow.Vault:
    """A kit nobody may open: reaching for one is the failure under test."""
    raise AssertionError('this push opened the kit, which the row it pushed does not need')


@functools.cache
def ca_pem() -> str:
    """One certificate authority for the whole file; generating a key is the slow part."""
    return pki.generate_ca_key()


@dataclass(frozen=True)
class Vault(escrow.Vault):
    """An escrow that recovers without a key, standing in for the kit's own.

    Every recovery is remembered -- the list is appended to, never rebound,
    which is all a frozen record allows -- so a test can hold a push to
    obtaining its value once.
    """

    recovered: list[str] = field(default_factory=list[str])

    def recover(self, label: str, generation: int | None = None) -> str:
        assert label in escrow.register() or label.startswith(escrow.BACKUP), label
        self.recovered.append(label)
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
    runner: pulumi_cli.Runner | None = None,
    ask: Callable[[str], str] | None = None,
    backend_url: str | None = None,
) -> slots.Context:
    """A push that reaches nothing real: no kit, no backend, no forge, no terminal."""
    # `physical`'s passphrase is there because the state reads are of that
    # stack, which is opened under its own and refused without it; the stack
    # passphrase, for the stacks a copy writes into.
    resolved = pulumi_config.BackendEnvironment(
        passphrase=STACK_PASSPHRASE, url=backend_url, physical=lambda: PHYSICAL_PASSPHRASE
    )
    return slots.Context(
        open_forge=lambda: Forge(token='the-admin-token', run=gh),
        open_vault=open_vault,
        open_environment=lambda: resolved,
        runner=runner if runner is not None else RecordedPulumi(),
        ask=ask if ask is not None else typing_in('typed-in'),
    )


def test_a_derived_row_is_recovered_once_and_pushed_to_every_slot() -> None:
    gh = RecordedGh()
    vault = opened()

    pushed = slots.sync(context(gh, open_vault=lambda: vault), only='pulumi-passphrase')

    # One recovery, a delivery per Environment: the value is obtained once and
    # fanned out, so a rotation is one command rather than one per
    # Environment.
    assert cast('Vault', vault).recovered == [escrow.PASSPHRASE]
    assert len(pushed) == len(STACK_PASSPHRASE_ENVIRONMENTS)
    for environment in STACK_PASSPHRASE_ENVIRONMENTS:
        assert gh.values[(REPOSITORY, environment, slots.PASSPHRASE_SECRET)] == PASSPHRASE
    for environment in slots.PHYSICAL_ENVIRONMENTS:
        assert (REPOSITORY, environment, slots.PASSPHRASE_SECRET) not in gh.values


def test_physicals_passphrase_is_recovered_and_pushed_into_physicals_environments_alone() -> None:
    """`sync --only physical-passphrase` is the delivery, and it writes nothing a pull request's jobs can read."""
    gh = RecordedGh()
    vault = opened()

    pushed = slots.sync(context(gh, open_vault=lambda: vault), only=stack_environment.PHYSICAL_PASSPHRASE_ROW)

    assert cast('Vault', vault).recovered == [escrow.PHYSICAL_PASSPHRASE]
    assert len(pushed) == len(slots.PHYSICAL_ENVIRONMENTS)
    assert set(gh.values) == {
        (REPOSITORY, environment, slots.PASSPHRASE_SECRET) for environment in slots.PHYSICAL_ENVIRONMENTS
    }


def test_the_trigger_app_key_is_recovered_once_and_pushed_to_the_ops_repository() -> None:
    """`sync --only github-trigger-key` is the delivery: recover from escrow, push, verify.

    The push goes to the ops repository and to no Environment of it, under
    the name the drift trigger reads, and the escrow is opened for that one
    label once. What is held is the invocation the sink makes; the forge
    itself is never reached from here.
    """
    gh = RecordedGh()
    vault = Vault(registry=escrow.Registry(root=Path('nowhere')), identity='not-an-identity')

    pushed = slots.sync(context(gh, open_vault=lambda: vault), only='github-trigger-key')

    assert vault.recovered == [escrow.TRIGGER_KEY]
    assert pushed == [str(Slot(repository=OPS_REPOSITORY, name=slots.TRIGGER_APP_KEY))]
    assert gh.values == {(OPS_REPOSITORY, None, slots.TRIGGER_APP_KEY): PASSPHRASE}
    # The listing read before and the verification after both scope the same
    # way as the write: `--repo` naming the ops repository, and no `--env`.
    for invocation in gh.invocations:
        assert invocation[invocation.index('--repo') + 1] == OPS_REPOSITORY, invocation
        assert '--env' not in invocation, invocation


def test_the_dispatch_app_key_is_recovered_once_and_pushed_to_the_deployment_repository() -> None:
    """`sync --only github-dispatch-key` is the delivery: recover from escrow, push, verify.

    The trigger key's push one repository over: the escrow is opened for the
    dispatch label once, and the one write goes to the deployment repository
    and to no Environment of it, under the name the alert producer reads.
    What is held is the invocation the sink makes; the forge itself is never
    reached from here.
    """
    gh = RecordedGh()
    vault = Vault(registry=escrow.Registry(root=Path('nowhere')), identity='not-an-identity')

    pushed = slots.sync(context(gh, open_vault=lambda: vault), only='github-dispatch-key')

    assert vault.recovered == [escrow.DISPATCH_KEY]
    assert pushed == [str(Slot(repository=REPOSITORY, name=slots.DISPATCH_APP_KEY))]
    assert gh.values == {(REPOSITORY, None, slots.DISPATCH_APP_KEY): PASSPHRASE}
    # The listing read before and the verification after both scope the same
    # way as the write: `--repo` naming the deployment repository, and no
    # `--env` -- the ops repository is never named by this row.
    for invocation in gh.invocations:
        assert invocation[invocation.index('--repo') + 1] == REPOSITORY, invocation
        assert '--env' not in invocation, invocation


def pushed_bundle(gh: RecordedGh, environment: str = ENVIRONMENTS[0]) -> dict[str, str]:
    """The bundle's carriers as they landed in one Environment."""
    return {name: gh.values[(REPOSITORY, environment, name)] for name in workflow_backend_secrets()}


def test_the_client_bundle_is_issued_once_and_split_across_its_carriers() -> None:
    gh = RecordedGh()

    pushed = slots.sync(
        context(gh, open_vault=opened, backend_url=OPERATOR_URL),
        only='state-backend-certificates',
    )

    assert len(pushed) == len(workflow_backend_secrets()) * len(ENVIRONMENTS)
    carriers = pushed_bundle(gh)
    # The certificate and the key that opens it come out of a single issuance,
    # so the two halves have to be a pair. Resolving the row twice would put
    # halves of two bundles into one Environment, and the box would refuse the
    # handshake with nothing to say about why.
    certificate = x509.load_pem_x509_certificate(carriers[slots.BACKEND_CERT].encode())
    key = serialization.load_pem_private_key(carriers[slots.BACKEND_KEY].encode(), password=None)
    assert certificate.public_key() == key.public_key()
    # The Common Name is the Postgres role the box maps the certificate to.
    assert certificate.subject.rfc4514_string() == f'CN={appliance_settings.CI_ROLE}'
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
    assert url.startswith(f'postgres://{appliance_settings.CI_ROLE}@{APPLIANCE}:')
    assert 'sslcert=' not in url and 'sslrootcert=' not in url and 'sslkey=' not in url


def test_every_environment_receives_the_same_bundle() -> None:
    gh = RecordedGh()

    _ = slots.sync(
        context(gh, open_vault=opened, backend_url=OPERATOR_URL),
        only='state-backend-certificates',
    )

    # One issuance fanned out, not one per Environment: five certificates would
    # be five things to reason about the day a handshake is refused.
    for environment in ENVIRONMENTS:
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


def test_a_sink_naming_a_part_the_bundle_does_not_carry_is_refused_by_name() -> None:
    bundle = slots.ROWS['state-backend-certificates']
    mistyped = slots.Row(
        register=bundle.register,
        source=bundle.source,
        targets=(Slot(repository=REPOSITORY, name='PULUMI_BACKEND_CERTIFICATE', environment=ENVIRONMENTS[0]),),
    )

    # A carrier misspelled, or a fifth one added without the part that fills it.
    # The row says which secret has no value and what it does deliver, rather
    # than failing on a lookup whose whole message is the name that was missing.
    with pytest.raises(SlotRefused, match='PULUMI_BACKEND_CERTIFICATE'):
        _ = mistyped.resolve(context(RecordedGh(), open_vault=opened, backend_url=OPERATOR_URL))


def test_a_backend_url_that_names_no_host_is_refused() -> None:
    gh = RecordedGh()
    broken = context(gh, open_vault=opened, backend_url='postgres:///pulumi_state')

    with pytest.raises(SlotRefused, match='names no host'):
        _ = slots.sync(broken, only='state-backend-certificates')


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
    assert gh.values == {(OPS_REPOSITORY, None, slots.HA_WEBHOOK_URL): 'typed-in'}
    assert pushed == []
    assert 'already in every slot' in caplog.text
    # The listing read before and the verification after both scope the same
    # way as the write: `--repo` naming the ops repository, and no `--env`.
    for invocation in gh.invocations:
        assert invocation[invocation.index('--repo') + 1] == OPS_REPOSITORY, invocation
        assert '--env' not in invocation, invocation


def test_naming_a_typed_in_row_replaces_what_is_there() -> None:
    gh = RecordedGh(collections={(OPS_REPOSITORY, None): {slots.HA_WEBHOOK_URL: '2026-01-01T00:00:00Z'}})

    pushed = slots.sync(context(gh, ask=typing_in('a-new-webhook')), only='haos-webhook')

    # Rotating it is a new webhook id in Home Assistant and this command, so
    # naming the row has to mean "replace" rather than "leave it".
    assert pushed
    assert gh.values == {(OPS_REPOSITORY, None, slots.HA_WEBHOOK_URL): 'a-new-webhook'}


def test_a_typed_in_row_that_is_left_empty_is_refused() -> None:
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='is required'):
        _ = slots.sync(context(gh, ask=typing_in('  ')), only='haos-webhook')


def test_a_state_read_pushes_the_output_the_map_names() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'], outputs={CI_DNS_IDENTITY: 'an-identity'})

    _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')

    # `--show-secrets`, because an identity is a secret output and without it
    # the string `[secret]` would be pushed as though it were the credential.
    (slot,) = slots.ROWS['zerotier-identity-dns'].sinks
    assert gh.values == {(slot.repository, slot.environment, slot.name): 'an-identity'}


def test_a_state_read_before_the_stack_exists_says_so() -> None:
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='has no state'):
        _ = slots.sync(context(gh, runner=RecordedPulumi()), only='zerotier-identity-dns')


def test_a_state_read_of_an_output_the_program_does_not_export_says_so() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'])

    # The output name is a contract the program has to keep -- it exports under
    # it or the read finds nothing -- so an unkept one is named rather than
    # pushed as an empty secret.
    with pytest.raises(SlotRefused, match='exports no'):
        _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')


def test_a_state_read_of_an_output_that_is_not_a_string_says_so() -> None:
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=['physical'], outputs={CI_DNS_IDENTITY: None})

    # A `null` output would otherwise be coerced into the four characters
    # `null` and pushed as though CI had been handed an identity.
    with pytest.raises(SlotRefused, match='must be a non-empty string'):
        _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')


def test_a_state_read_of_the_unknown_sentinel_says_the_apply_was_targeted() -> None:
    gh = RecordedGh()
    # The literal Pulumi writes for an unknown, spelled out rather than imported
    # from `pulumi.runtime.rpc`: a test that pins a constant against itself pins
    # nothing, and this string is the interface the checkpoint carries.
    sentinel = '04da6b54-80e4-46f7-96ec-b56ff0331ba9'
    pulumi = RecordedPulumi(stacks=['physical'], outputs={CI_DNS_IDENTITY: sentinel})

    # An export whose value comes from a resource a `--target`ed apply skipped
    # is a string, so the type boundary above lets it through: it is present,
    # well-typed and meaningless, and pushing it fills the slot with a value CI
    # would only discover to be worthless when a workflow tried to use it.
    with pytest.raises(SlotRefused, match='unknown sentinel'):
        _ = slots.sync(context(gh, runner=pulumi), only='zerotier-identity-dns')

    assert not gh.values


#: The cluster-admin kubeconfig as `physical` exports it: a YAML document,
#: ending in the newline the channel it is copied into does not keep.
KUBECONFIG = 'apiVersion: v1\nkind: Config\nclusters: [a-fake-cluster-nothing-reaches]\n'
K8S_BASE_STACK = conventions.STACK_NAMES.k8s_base
APPS_STACK = conventions.STACK_NAMES.apps
KUBECONFIG_OUTPUT = conventions.PHYSICAL_OUTPUTS.kubeconfig


def no_forge() -> Forge:
    """A forge nobody may open: copying a row with no GitHub slot must not read the admin token."""
    raise AssertionError('the copy opened the forge, which a row with no GitHub slot does not need')


def kubeconfig_copy(pulumi: RecordedPulumi) -> slots.Context:
    """The context `sync --only kubeconfig` runs in, with a forge that refuses to open."""
    copying = context(RecordedGh(), runner=pulumi)
    copying.open_forge = no_forge
    return copying


def published(held: Mapping[str, str] | None = None, *, plain: tuple[str, ...] = ()) -> RecordedPulumi:
    """`physical` applied and exporting the kubeconfig, both reading stacks there, and `held` already in them.

    `held` maps a stack to the copy it holds; `plain` names the stacks whose
    copy is stored in the clear rather than as a secret.
    """
    return RecordedPulumi(
        stacks=[PHYSICAL_STACK, K8S_BASE_STACK, APPS_STACK],
        outputs={KUBECONFIG_OUTPUT: KUBECONFIG},
        config={(stack, slots.KUBECONFIG_KEY): value for stack, value in (held or {}).items()},
        plain={(stack, slots.KUBECONFIG_KEY) for stack in plain},
    )


def test_the_kubeconfig_is_copied_out_of_physical_into_both_stacks_that_read_it() -> None:
    """`sync --only kubeconfig` reads `physical`'s state and writes both stacks' configuration.

    Read under `physical`'s own passphrase and written under the stack
    passphrase, which is the whole reason for the copy: neither reading stack
    can open `physical`'s secrets (rfc-005 §5.1). Written stripped, because the
    channel drops the newline the document ends in and a read-back that kept it
    would never match. And no admin token is read: the row has no GitHub slot.
    """
    pulumi = published()

    pushed = slots.sync(kubeconfig_copy(pulumi), only='kubeconfig')

    assert pulumi.config == {
        (K8S_BASE_STACK, slots.KUBECONFIG_KEY): KUBECONFIG.strip(),
        (APPS_STACK, slots.KUBECONFIG_KEY): KUBECONFIG.strip(),
    }
    assert pushed == [
        str(slots.PulumiConfig(K8S_BASE_STACK, slots.KUBECONFIG_KEY)),
        str(slots.PulumiConfig(APPS_STACK, slots.KUBECONFIG_KEY)),
    ]
    read = [passphrase for command, passphrase in pulumi.passphrases if command.startswith('stack output')]
    written = [passphrase for command, passphrase in pulumi.passphrases if command.startswith('config set')]
    assert read == [PHYSICAL_PASSPHRASE]
    assert written == [STACK_PASSPHRASE, STACK_PASSPHRASE]


def test_a_kubeconfig_physical_has_not_published_is_refused_by_name_and_copied_nowhere() -> None:
    pulumi = published()
    pulumi.outputs = {}

    with pytest.raises(SlotRefused, match=f'exports no `{KUBECONFIG_OUTPUT}`'):
        _ = slots.sync(kubeconfig_copy(pulumi), only='kubeconfig')

    assert pulumi.config == {}


def test_a_copy_into_a_stack_that_does_not_exist_is_refused_before_physical_is_read() -> None:
    """A copy fills a stack that is there; creating one is that stack's own bring-up."""
    pulumi = published()
    pulumi.stacks.remove(APPS_STACK)

    with pytest.raises(SlotRefused, match=f'`pulumi stack init {APPS_STACK} --no-select`'):
        _ = slots.sync(kubeconfig_copy(pulumi), only='kubeconfig')

    assert pulumi.config == {}
    assert not [command for command in pulumi.invocations if command[:2] == ['stack', 'output']]
    assert not [command for command in pulumi.invocations if command[:2] == ['stack', 'init']]


def test_a_stack_already_holding_the_kubeconfig_is_left_alone() -> None:
    """A copy of an unchanged value writes nothing: every write is fresh ciphertext, a diff that changes nothing."""
    pulumi = published({K8S_BASE_STACK: KUBECONFIG.strip(), APPS_STACK: 'the-copy-physical-rotated-away'})

    pushed = slots.sync(kubeconfig_copy(pulumi), only='kubeconfig')

    written = [command for command in pulumi.invocations if command[:2] == ['config', 'set']]
    assert written == [['config', 'set', slots.KUBECONFIG_KEY, '--secret', '--stack', APPS_STACK]]
    assert pushed == [str(slots.PulumiConfig(APPS_STACK, slots.KUBECONFIG_KEY))]
    assert pulumi.config[(APPS_STACK, slots.KUBECONFIG_KEY)] == KUBECONFIG.strip()


def test_a_copy_held_in_the_clear_is_written_again_as_a_secret() -> None:
    """The same text stored without encryption is not a copy to leave alone.

    Its decrypted value matches, so only how it is stored tells it apart, and
    a cluster-admin credential in the clear in a committed file is one the
    command that makes the copy repairs rather than certifies.
    """
    pulumi = published({K8S_BASE_STACK: KUBECONFIG.strip(), APPS_STACK: KUBECONFIG.strip()}, plain=(K8S_BASE_STACK,))

    pushed = slots.sync(kubeconfig_copy(pulumi), only='kubeconfig')

    assert pushed == [str(slots.PulumiConfig(K8S_BASE_STACK, slots.KUBECONFIG_KEY))]
    assert pulumi.plain == set()


def test_the_kubeconfig_copy_prints_nothing_of_the_value(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    # One stack written and one left alone, so both of the copy's log lines run.
    pulumi = published({K8S_BASE_STACK: KUBECONFIG.strip()})

    _ = slots.sync(kubeconfig_copy(pulumi), only='kubeconfig')

    assert 'a-fake-cluster-nothing-reaches' not in caplog.text
    # Nor does it say it reads the forge, which a row with no GitHub slot does not.
    assert 'forge' not in caplog.text
    assert f'the {K8S_BASE_STACK} stack already holds it' in caplog.text
    assert f'commit Pulumi.{APPS_STACK}.yaml' in caplog.text


def test_a_typed_in_row_s_config_slots_are_left_to_its_own_command() -> None:
    """A device row's config secrets are filled by its `record`, so `sync` neither asks for it nor writes them."""
    gh = RecordedGh()
    pulumi = RecordedPulumi(stacks=[PHYSICAL_STACK])

    def never(_prompt: str) -> str:
        raise AssertionError('sync asked for a value its own record command takes')

    with pytest.raises(SlotRefused, match='no GitHub secret slot'):
        _ = slots.sync(context(gh, runner=pulumi, ask=never), only='unifi')

    assert pulumi.invocations == []


def test_the_network_id_is_pushed_from_the_constant_that_decides_it() -> None:
    gh = RecordedGh()

    pushed = slots.sync(context(gh), only='zerotier-network')

    # Not a credential, but a workflow input that has nowhere else to come from
    # and can only be passed as a secret. Its value is the adopted network's
    # identity, which is a constant rather than a configured one, so this row
    # opens no stack at all.
    sinks = slots.ROWS['zerotier-network'].sinks
    assert pushed == [str(slot) for slot in sinks]
    assert gh.values == {
        (slot.repository, slot.environment, slot.name): conventions.overlay.NETWORK_ID for slot in sinks
    }


def test_a_minted_row_refuses_by_naming_the_command_that_delivers_it() -> None:
    # A minted credential is disclosed once, to the call that made it, so this
    # map can only say where it goes and who puts it there. The command named
    # is held to the tree rather than to the row's own spelling of it, which a
    # misspelled row would repeat.
    name = 'oci-physical'

    with pytest.raises(SlotRefused) as refusal:
        _ = slots.ROWS[name].resolve(context(RecordedGh()))

    named = named_commands(str(refusal.value))
    assert named, f'the refusal names no `credentials` command: {refusal.value}'
    outside = [argv for argv, parsed in named if parsed['member'] != name]
    assert outside == [], f'the refusal names a command outside {name}: {refusal.value}'


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


#: A row with no GitHub slot, waiting on two channels: the case the two tests
#: below are about, set up here rather than borrowed from whichever row of the
#: map is waiting today.
WAITING = slots.Row(
    register='a credential',
    source=slots.Derived('a-label'),
    targets=(slots.PulumiState('a-stack', 'a value'),),
    pending={
        'ops-repo secret': 'the workflow that would read it is not built',
        'SealedSecret': 'the controller that would open it is not installed',
    },
)


def test_a_row_with_no_github_slot_is_skipped_with_its_reason(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    gh = RecordedGh()

    pushed = slots.sync(context(gh), rows={'waiting': WAITING})

    assert pushed == []
    assert gh.invocations == []
    # Under the channel it is about: the operator reads which slot is waiting,
    # not a sentence they have to match to a cell of §3 themselves.
    for channel, why in WAITING.pending.items():
        assert f'{channel}: {why}' in caplog.text


def test_a_row_that_cannot_produce_its_value_does_not_stop_the_walk(caplog: pytest.LogCaptureFixture) -> None:
    gh = RecordedGh()
    both = {name: slots.ROWS[name] for name in ('zerotier-identity-dns', 'pulumi-passphrase')}

    # Most of the map is waiting on something, so a whole-map run has to deliver
    # what it can and report the rest — and still exit non-zero, because the map
    # is not filled.
    with pytest.raises(SlotRefused, match='zerotier-identity-dns'):
        _ = slots.sync(context(gh, open_vault=opened), rows=both)

    assert gh.values == {
        (slot.repository, slot.environment, slot.name): PASSPHRASE for slot in slots.ROWS['pulumi-passphrase'].sinks
    }
    assert 'has no state' in caplog.text


def test_naming_a_row_with_no_github_slot_is_refused_rather_than_ignored() -> None:
    # A request for one specific row that quietly does nothing is worse than a
    # refusal: the operator walks away believing the slot is filled.
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='no GitHub secret slot'):
        _ = slots.sync(context(gh), rows={'waiting': WAITING}, only='waiting')

    assert gh.invocations == []


def test_an_unknown_row_name_is_refused_before_anything_is_pushed() -> None:
    gh = RecordedGh()

    with pytest.raises(SlotRefused, match='no slot map row named'):
        _ = slots.sync(context(gh), only='nonesuch')

    assert gh.invocations == []


@pytest.mark.parametrize('name', sorted(slots.RETIRED))
def test_a_retired_name_answers_with_where_the_fact_lives_now(name: str) -> None:
    gh = RecordedGh()

    # Someone working from a runbook older than the move gets the new home
    # rather than "no such row", which is true and tells them nothing.
    with pytest.raises(SlotRefused) as refusal:
        _ = slots.sync(context(gh), only=name)

    assert slots.RETIRED[name] in str(refusal.value)
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
        for channel, why in row.pending.items():
            assert f'{channel}: {why}' in printed


def test_a_stack_encrypted_apart_has_a_row_that_generates_its_passphrase() -> None:
    """The census in `pulumi_config` cannot name a stack the register does not serve.

    A stack taken off the stack passphrase needs somewhere for its own to come
    from and somewhere for it to be recovered from; naming one in `APART` with
    no such row would make every command against that stack refuse forever.
    """
    for stack, row in pulumi_config.APART.items():
        assert row in slots.ROWS, f'{stack} is encrypted apart but {row} is no row of the map'
        assert isinstance(slots.ROWS[row].source, slots.Derived), (
            f'{row} has to be recoverable from the kit: it is the only way back into the {stack} stack'
        )


def test_the_passphrase_of_a_stack_encrypted_apart_reaches_no_github_secret() -> None:
    """The property the operator passphrase exists for, held rather than merely true.

    Every Environment holds a passphrase under `PULUMI_CONFIG_PASSPHRASE`
    because every job runs a `pulumi` command -- the stack passphrase, or
    `physical`'s own in its two. This one is in none, which is what keeps the operator
    stacks' config -- the `github` stack's admin token that can unguard `main`
    among it -- unreadable by anything CI can start. A sink added to that row
    would undo it silently, so the emptiness is the assertion.
    """
    assert pulumi_config.APART, 'nothing to check: no stack is encrypted apart from the others'
    for name in pulumi_config.APART.values():
        row = slots.ROWS[name]

        assert row.sinks == (), 'an operator passphrase that reaches a GitHub secret is the stack passphrase again'
        assert row.pending == {}, 'no channel is waiting: reaching no CI secret is the design, not a gap'

    # The contrast, so this cannot pass by the map having lost its GitHub
    # secrets altogether.
    assert any(row.sinks for row in slots.ROWS.values())


def test_the_operator_passphrase_lands_where_its_chain_reads_it() -> None:
    """The store first and the slot beside it, under the names the driver's chain reads and `recover` writes.

    The map names the channels and the escrow row carries the addresses
    `recover` writes to; both are held to the names `stack_environment` finds
    the passphrase under, so none of the three can move alone.
    """
    row = slots.ROWS[stack_environment.OPERATOR_PASSPHRASE_ROW]
    written = escrow.slot(escrow.OPERATOR_PASSPHRASE)

    assert slots.SecretStore(stack_environment.OPERATOR_PASSPHRASE_ACCOUNT) in row.targets
    assert slots.WorkstationSlot(stack_environment.OPERATOR_PASSPHRASE_SLOT) in row.targets
    assert written is not None
    assert written.store == stack_environment.OPERATOR_PASSPHRASE_ACCOUNT
    assert written.path().name == stack_environment.OPERATOR_PASSPHRASE_SLOT
    # And the stack passphrase, which a `mise.toml` template reads, has a file
    # and no store.
    stack = escrow.slot(escrow.PASSPHRASE)
    assert stack is not None
    assert stack.store is None


def test_a_sealed_row_lands_at_the_census_row_its_value_is() -> None:
    sealed = {
        name: [target.what for target in row.targets if isinstance(target, slots.SealedSecret)]
        for name, row in slots.ROWS.items()
    }

    # Every sealed value the census carries is delivered by exactly one row,
    # and a row's sealed target is a census row rather than a description: the
    # command that seals it and the program that reads it find the place by
    # the same row.
    delivered = [value for values in sealed.values() for value in values]
    assert sorted(value.name for value in delivered) == sorted(conventions.sealed.VALUES)
