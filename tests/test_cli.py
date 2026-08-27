"""Every leaf of the command tree reaches the handler its shape implies.

The tree is generated from the registers (`entries.SEEDS`, `masters.ROOTS`),
so the test that covers it is generated too: it walks the real parser, builds
the shortest argument vector that reaches each leaf, and drives it through the
real `main`. A register row nothing can dispatch — or a subparser added
without a `case` for it — fails here rather than on an operator's first run,
and a row added later is covered without anyone editing this file.

What each leaf is expected to reach is derived from its own path, not from a
copy of today's tree: `seed <member> create` must reach the one minting
entrypoint, `seed <member> rotate` must reach the module that owns that
member or refuse by name, `master <root> <action>` must reach the account-root
handler of that name. A row's `repair` action is the one shape that cannot be
derived — it exists for one row and does one thing — so it is named here, and
the walk still proves it is reachable. The handlers themselves are stubbed; what is under test
is the dispatch, not what the dispatch calls.
"""

from __future__ import annotations

import argparse
import io
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from kluster.scripts.credentials import cli, entries, masters
from kluster.scripts.credentials.kdbx import PATH_ENV, KdbxStore

PASSWORD = 'kit-password'

#: The refusal a register row with no implementation produces (`cli.main`).
REFUSAL = 'not yet implemented'


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[argparse.ArgumentParser] | None:  # pyright: ignore[reportPrivateUsage]
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            return cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)  # pyright: ignore[reportPrivateUsage]
    return None


def _fill(parser: argparse.ArgumentParser, into: list[str]) -> None:
    """Append whatever this level insists on, so a leaf can be reached at all.

    Values are placeholders: every handler is stubbed, so only the dispatch
    sees them. Required-ness is read off the parser rather than listed here,
    which is what keeps a newly required option from silently going untested.
    """
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction | argparse._HelpAction):  # pyright: ignore[reportPrivateUsage]
            continue
        placeholder = 'placeholder.kdbx' if action.type is Path else 'placeholder'
        if action.option_strings:
            if action.required:
                into.extend((action.option_strings[0], placeholder))
        elif action.nargs not in ('?', '*'):
            into.append(placeholder)


def leaves(parser: argparse.ArgumentParser) -> Iterator[list[str]]:
    """Every command that can actually be run, as the argv that runs it."""
    prefix: list[str] = []
    _fill(parser, prefix)
    subparsers = _subparsers(parser)
    if subparsers is None:
        yield prefix
        return
    for name, child in subparsers.choices.items():
        for tail in leaves(child):
            yield [*prefix, name, *tail]


def commands() -> list[list[str]]:
    return list(leaves(cli.build_parser()))


def _module_for(member: str) -> types.ModuleType | None:
    """The module that owns a seed member, found the way `cli` imports it.

    `oci` is served by `oci_iam`, so the match is by prefix; a member with no
    module is a register row whose implementation does not exist yet.
    """
    wanted = member.replace('-', '_')
    for value in vars(cli).values():
        if isinstance(value, types.ModuleType) and value.__name__.rsplit('.', 1)[-1].startswith(wanted):
            if hasattr(value, 'rotate_seed'):
                return value
    return None


def expected(path: list[str]) -> str | None:
    """Which stub this leaf must reach, or None if it must refuse by name."""
    match [part for part in path if not part.startswith('-') and not part.startswith('placeholder')]:
        case ['master', 'ls']:
            return 'masters.stored'
        case ['master', _, action]:
            return f'masters.{action}'
        case ['kdbx', 'ls']:
            return 'store.entries'
        case ['kdbx', 'show']:
            return 'store.describe'
        case ['kdbx', action]:
            return f'store.{action}'
        case ['derived', row, credential]:
            # `state-backend` is a row name and not an identifier, which is the
            # one place the two spellings differ.
            return f'derived.{row}_{credential}'.replace('-', '_')
        case ['escrow', 'env']:
            return 'lifecycle.environment'
        case ['escrow', 'recover']:
            return 'escrow.Vault.open'
        case ['escrow', 'import']:
            # `import` is a keyword, so the register's verb and the function
            # that implements it cannot share a name.
            return 'escrow.adopt'
        case ['escrow', action]:
            return f'escrow.{action}'
        case ['bootstrap']:
            return 'lifecycle.bootstrap'
        case ['rotate']:
            return 'lifecycle.rotate'
        case ['seed', _, 'create']:
            return 'lifecycle.create_seed'
        case ['seed', member, 'rotate']:
            module = _module_for(member)
            return None if module is None else f'{module.__name__.rsplit(".", 1)[-1]}.rotate_seed'
        case ['seed', 'oci', 'domain']:
            return 'oci_iam.adopt_domain'
        case unknown:  # pragma: no cover - a leaf shape this test has no rule for
            raise AssertionError(f'no expectation for {unknown}; the tree grew a shape the walk does not know')


@pytest.fixture(scope='module')
def kit(tmp_path_factory: pytest.TempPathFactory) -> KdbxStore:
    """One real database for the whole module: creating one is the slow part."""
    return KdbxStore.create(tmp_path_factory.mktemp('cli') / 'kit.kdbx', PASSWORD)


class _Vault:
    """Stands in for an opened escrow; every leaf that reaches one is a dispatch."""

    def recover(self, _label: str, _generation: int | None = None) -> str:
        return 'a-secret'


class Dispatch:
    """Every handler `main` can reach, replaced by a recorder.

    Recording the name rather than asserting inside each stub is what lets the
    expectation be computed from the leaf's path: the test compares two
    derived strings instead of maintaining a table.
    """

    def __init__(self) -> None:
        self.reached: list[str] = []
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def stub(self, name: str, result: Any = None) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.reached.append(name)
            self.calls.append((name, args, kwargs))
            return result

        return record

    def install(self, monkeypatch: pytest.MonkeyPatch, kit: KdbxStore) -> None:
        handlers: tuple[tuple[types.ModuleType, str, Any], ...] = (
            (cli.lifecycle, 'bootstrap', []),
            (cli.lifecycle, 'rotate', []),
            (cli.lifecycle, 'create_seed', None),
            (cli.lifecycle, 'environment', {}),
            (cli.escrow, 'init', cli.escrow.age.Identity(secret='a-secret', public='age1stub')),
            (cli.escrow, 'generate', 'a-secret'),
            (cli.escrow, 'adopt', Path('placeholder')),
            (cli.escrow, 'rewrap', []),
            (cli.escrow, 'check', []),
            (cli.escrow, 'missing', []),
            (cli.masters, 'stored', {}),
            (cli.masters, 'remember', []),
            (cli.masters, 'forget', None),
            (cli.lifecycle, 'root', None),
            (cli.oci_iam, 'rotate_seed', 'fingerprint'),
            (cli.oci_iam, 'adopt_domain', 'https://domain.example'),
            (cli.b2, 'rotate_seed', 'key-id'),
            (cli.derived, 'cloudflare_zones', 'account-id'),
            (cli.derived, 'oci_physical', 'ocid1.user.test'),
            (cli.derived, 'oci_state_backend', Path('placeholder')),
            (cli.derived, 'b2_management', 'key-id'),
            # Slots are files in the checkout this test is running from, so
            # the writer is stubbed: a dispatch test must not leave a
            # placeholder passphrase where mise would then read it.
            (cli.workstation, 'write', Path('placeholder')),
        )
        for module, attribute, result in handlers:
            name = f'{module.__name__.rsplit(".", 1)[-1]}.{attribute}'
            monkeypatch.setattr(module, attribute, self.stub(name, result))
        # A vault that opens nothing: what is under test is the dispatch, and
        # a real one would want a real recovery key.
        monkeypatch.setattr(KdbxStore, 'get', self.stub('store.get', 'a-secret'))
        monkeypatch.setattr(cli.escrow.Vault, 'open', classmethod(self.stub('escrow.Vault.open', _Vault())))
        methods: tuple[tuple[str, Any], ...] = (
            ('entries', []),
            ('describe', {}),
            ('remember', None),
            ('forget', None),
            ('unlock_with', None),
        )
        for attribute, result in methods:
            monkeypatch.setattr(KdbxStore, attribute, self.stub(f'store.{attribute}', result))
        # `rotate` creates the successor database, and `bootstrap` creates the
        # kit when there is none; neither should write a file here.
        monkeypatch.setattr(KdbxStore, 'create', self.stub('store.create', kit))
        monkeypatch.setattr('getpass.getpass', lambda _prompt='': PASSWORD)
        # `escrow env` and `escrow recover` refuse to print a secret to a
        # terminal, and whether the test runner has one is not this test's
        # business; `escrow import` reads its value from standard input.
        monkeypatch.setattr('sys.stdout', io.StringIO())
        monkeypatch.setattr('sys.stdin', io.StringIO('a-value'))


@pytest.fixture
def dispatch(kit: KdbxStore, monkeypatch: pytest.MonkeyPatch) -> Dispatch:
    monkeypatch.setenv(PATH_ENV, str(kit.path))
    recorder = Dispatch()
    recorder.install(monkeypatch, kit)
    return recorder


def test_the_walk_finds_every_register_row() -> None:
    # The walk is only worth anything if it really enumerates the registers:
    # every seed has a `create`, every self-reproducing one a `rotate`, every
    # account root its two actions.
    found = commands()

    for member, seed in entries.SEEDS.items():
        assert ['seed', member, 'create'] in found
        assert (['seed', member, 'rotate'] in found) == seed.self_reproducing
        if seed.repair is not None:
            assert ['seed', member, seed.repair[0]] in found
    for member in masters.ROOTS:
        assert ['master', member, 'remember'] in found
    assert ['bootstrap'] in found


@pytest.mark.parametrize('argv', commands(), ids=lambda argv: ' '.join(argv))
def test_every_leaf_dispatches(argv: list[str], dispatch: Dispatch, caplog: pytest.LogCaptureFixture) -> None:
    target = expected(argv)

    code = cli.main(argv)

    if target is None:
        # A register row with no implementation is a subcommand that refuses
        # by name, which is the documented behaviour, not a crash.
        assert code == 1
        assert REFUSAL in caplog.text
        return
    assert code == 0, caplog.text
    assert target in dispatch.reached


def test_bootstrap_carries_its_only_through(dispatch: Dispatch) -> None:
    assert cli.main(['bootstrap', '--only', 'recovery']) == 0

    # `bootstrap` and `rotate` have no <action> level, so their namespaces
    # lack the attribute the dispatch matches on; the arguments they do carry
    # have to survive that.
    assert [kwargs['only'] for name, _, kwargs in dispatch.calls if name == 'lifecycle.bootstrap'] == ['recovery']


def test_rotate_carries_its_only_and_its_destination_through(dispatch: Dispatch, tmp_path: Path) -> None:
    successor = tmp_path / 'next.kdbx'

    assert cli.main(['rotate', '--into', str(successor), '--only', 'oci']) == 0

    assert [args[0] for name, args, _ in dispatch.calls if name == 'store.create'] == [successor]
    assert [kwargs['only'] for name, _, kwargs in dispatch.calls if name == 'lifecycle.rotate'] == ['oci']


def test_rotate_refuses_an_unknown_member_before_creating_the_successor(
    dispatch: Dispatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The command owns the successor file, not `rotate`, so a `--only` nothing
    # answers has to be refused here or the typo leaves an empty kit behind.
    assert cli.main(['rotate', '--into', str(tmp_path / 'next.kdbx'), '--only', 'nonesuch']) == 1

    assert 'no seed named' in caplog.text
    assert 'store.create' not in dispatch.reached
    assert 'lifecycle.rotate' not in dispatch.reached


def test_the_zones_row_is_pushed_into_the_stack_it_names(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'cloudflare', 'zones', '--stack', 'elsewhere']) == 0

    # The slot is built by the dispatch, so the stack it names and the state
    # the push opens have to arrive at the row's own function.
    (_, _, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'derived.cloudflare_zones']
    assert not rest
    assert kwargs['stack'].name == 'elsewhere'
    assert 'lifecycle.environment' in dispatch.reached


def test_a_row_named_after_its_consumer_takes_no_stack_of_its_own() -> None:
    # What these two mint is named after the row -- one IAM user, one B2 key
    # name -- and the mint retires every other credential of that name, so a
    # `--stack` would revoke the real stack's live credential on its way to
    # filling a different stack's slot. The flag is absent rather than
    # documented as dangerous.
    for argv in (
        ['derived', 'oci', 'physical', '--compartment', 'ocid1.compartment.test', '--stack', 'elsewhere'],
        ['derived', 'b2', 'management', '--stack', 'elsewhere'],
    ):
        with pytest.raises(SystemExit):
            _ = cli.build_parser().parse_args(argv)


def test_the_fixed_rows_are_pushed_into_the_stack_they_are_named_after(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'oci', 'physical', '--compartment', 'ocid1.compartment.test']) == 0
    assert cli.main(['derived', 'b2', 'management']) == 0

    pushed = [kwargs['stack'].name for name, _, kwargs in dispatch.calls if name.startswith('derived.')]
    assert pushed == [cli.derived.PHYSICAL_STACK, cli.derived.PHYSICAL_STACK]


def test_the_compartment_reaches_the_row_that_confines_the_key_with_it(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'oci', 'physical', '--compartment', 'ocid1.compartment.test']) == 0

    # The policy the mint writes names this compartment, so a command that
    # dropped it would mint a key confined to somewhere else entirely.
    (_, _, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'derived.oci_physical']
    assert not rest
    assert kwargs['compartment_id'] == 'ocid1.compartment.test'


def test_the_passphrase_is_written_to_its_slot_rather_than_redirected(dispatch: Dispatch) -> None:
    assert cli.main(['escrow', 'recover', 'pulumi/passphrase']) == 0

    # The command owns the file, so it is 0600 from the moment it exists
    # instead of whatever the shell's umask happened to be.
    (_, args, _), *rest = [call for call in dispatch.calls if call[0] == 'workstation.write']
    assert not rest
    assert args[0] == cli.workstation.passphrase_path()
    assert args[1] == 'a-secret'


def test_generating_the_passphrase_also_fills_its_slot(dispatch: Dispatch) -> None:
    # generate -> escrow -> push: the value reaches the slot mise.toml reads
    # in the same run, so a rotation is one command rather than two.
    assert cli.main(['escrow', 'generate', 'pulumi/passphrase']) == 0

    (_, args, _), *rest = [call for call in dispatch.calls if call[0] == 'workstation.write']
    assert not rest
    assert args[0] == cli.workstation.passphrase_path()
    assert args[1] == 'a-secret'


def test_generating_a_label_with_no_slot_writes_no_file(dispatch: Dispatch) -> None:
    assert cli.main(['escrow', 'generate', 'state-backend/ca']) == 0

    assert 'workstation.write' not in dispatch.reached


def test_the_passphrase_can_still_be_piped_to_another_machine(dispatch: Dispatch) -> None:
    assert cli.main(['escrow', 'recover', 'pulumi/passphrase', '--stdout']) == 0

    assert 'workstation.write' not in dispatch.reached


def test_import_refuses_an_empty_pipe_and_says_where_it_came_from(
    dispatch: Dispatch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The failure this exists for: `producer | credentials escrow import ...`
    # where the producer died. Import still gets a value, and escrowing it
    # files an empty string as a generation nobody notices until a recovery.
    monkeypatch.setattr('sys.stdin', io.StringIO('\n'))

    assert cli.main(['escrow', 'import', 'pulumi/passphrase']) == 1

    assert 'standard input was empty' in caplog.text
    assert 'escrow.adopt' not in dispatch.reached


def test_import_refuses_an_empty_slot(
    dispatch: Dispatch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / 'passphrase'
    _ = empty.write_text('')
    monkeypatch.setitem(cli.escrow.SLOTS, 'pulumi/passphrase', lambda: empty)

    assert cli.main(['escrow', 'import', 'pulumi/passphrase', '--from-slot']) == 1

    assert str(empty) in caplog.text
    assert 'escrow.adopt' not in dispatch.reached


def test_check_runs_without_opening_a_kit(dispatch: Dispatch) -> None:
    # The one command a stranger with a clone can run. Opening the kit here
    # would make a check into a ceremony.
    assert cli.main(['escrow', 'check']) == 0

    assert 'escrow.check' in dispatch.reached
    assert not [name for name in dispatch.reached if name.startswith('store.')]


def test_seed_create_dispatches_the_row_the_member_names(dispatch: Dispatch) -> None:
    assert cli.main(['seed', 'oci', 'create']) == 0

    # One row at a time goes through the same entrypoint `bootstrap` walks,
    # with the register's own row rather than a name looked up again inside.
    (_, args, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'lifecycle.create_seed']
    assert not rest
    assert args[0] is entries.SEEDS['oci']
    assert kwargs['entry'] == entries.SEEDS['oci'].entry
