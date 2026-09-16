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
member or refuse by name, `root <name> <verb>` must reach the account-root
handler of that name, and `derived <row> mint` must reach the function whose
identifier is that row name with `_` for `-`. A row's `repair` action is the one shape that cannot be
derived — it exists for one row and does one thing — so it is named here, and
the walk still proves it is reachable. The handlers themselves are stubbed; what is under test
is the dispatch, not what the dispatch calls.

The same walk carries a second property: the global `--escrow` reaches every
command that opens an escrow. Which those are is read off the handlers'
signatures rather than listed, so neither half of that census is a list of
names anyone has to keep.
"""

from __future__ import annotations

import argparse
import inspect
import io
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from kluster.scripts.credentials import cli, devices, entries, escrow, masters
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
        case ['root', 'ls']:
            return 'masters.stored'
        case ['root', _, action]:
            return f'masters.{action}'
        case ['kit', 'ls']:
            return 'store.entries'
        case ['kit', 'show']:
            return 'store.describe'
        case ['kit', 'password', verb]:
            return f'store.{verb}'
        case ['kit', 'rewrap']:
            return 'escrow.rewrap'
        case ['kit', verb]:
            return f'lifecycle.{verb}'
        case ['derived', 'ls']:
            # `ls` prints the map, and the map's own module is where the
            # register's machine-readable half lives.
            return 'slots.describe'
        case ['derived', 'check']:
            return 'escrow.check'
        case ['derived', 'sync']:
            return 'slots.sync'
        case ['derived', row, 'mint']:
            # The row name is the function's identifier with `-` for `_`, which
            # is the whole of the convention tying the tree to `derived.py`.
            return f'derived.{row}'.replace('-', '_')
        case ['derived', cli.derived.DRILL_AGE_IDENTITY_ROW as row, 'generate']:
            # The one `generate` that escrows nothing: drawn and delivered by
            # its own function in `derived.py`, under the mint rows' convention.
            return f'derived.{row}'.replace('-', '_')
        case ['derived', row, 'record'] if row in devices.DEVICES:
            # One handler for every device row: what differs between them is
            # the table in `devices.py`, not the code that reads it.
            return 'devices.deliver'
        case ['derived', _, 'record']:
            # The same verb for a console-made row whose consumer is not a
            # stack: what it is delivered into is the escrow, so the writer is
            # the registry's rather than the config slot's.
            return 'escrow.record'
        case ['derived', _, 'recover']:
            return 'escrow.Vault.open'
        case ['derived', _, 'import']:
            # `import` is a keyword, so the register's verb and the function
            # that implements it cannot share a name.
            return 'escrow.adopt'
        case ['derived', _, verb]:
            return f'escrow.{verb}'
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
        #: The real signature of each handler, taken before it is replaced, so
        #: that what a stub was handed can be read by parameter name rather
        #: than by position.
        self.signatures: dict[str, inspect.Signature] = {}

    def stub(self, name: str, result: Any = None) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> Any:
            self.reached.append(name)
            self.calls.append((name, args, kwargs))
            return result

        return record

    def registries(self, calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]]) -> list[tuple[str, Any]]:
        """The escrow each of these calls was handed, by handler name.

        Which handlers open an escrow is read off their own signatures rather
        than listed here: the directory `--escrow` names travels as a
        parameter called `registry`, so a handler that has one is a handler
        the census below covers, and one written later is covered with it.
        Defaults are applied, because a caller that passes nothing is exactly
        the failure the census exists to catch — it reads as `None`, which is
        the handler's own instruction to open the checkout's `escrow/`.
        """
        given: list[tuple[str, Any]] = []
        for name, args, kwargs in calls:
            signature = self.signatures[name]
            if 'registry' not in signature.parameters:
                continue
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            given.append((name, bound.arguments['registry']))
        return given

    def install(self, monkeypatch: pytest.MonkeyPatch, kit: KdbxStore) -> None:
        handlers: tuple[tuple[types.ModuleType, str, Any], ...] = (
            (cli.lifecycle, 'bootstrap', []),
            (cli.lifecycle, 'rotate', []),
            (cli.lifecycle, 'create_seed', None),
            (cli.lifecycle, 'environment', cli.pulumi_config.BackendEnvironment()),
            (cli.escrow, 'generate', 'a-secret'),
            (cli.escrow, 'adopt', Path('placeholder')),
            (cli.escrow, 'record', Path('placeholder')),
            (cli.escrow, 'from_kit', 'a-recorded-key'),
            (cli.escrow, 'rewrap', []),
            (cli.escrow, 'check', []),
            (cli.escrow, 'missing', []),
            (cli.masters, 'stored', {}),
            (cli.masters, 'remember', []),
            (cli.masters, 'forget', None),
            # The account root a mint borrows.
            (cli.lifecycle, 'root', masters.Credential(root=masters.ROOTS['oci'], values={'tenancy': 'ocid1.a'})),
            # The admin token a push authenticates as. A value rather than
            # `None`, because the GitHub sink builds the forge it pushes
            # through out of it, and it is read from the `github` stack's
            # committed config rather than from a root (`devices.py`).
            (cli.devices, 'borrow', 'a-token'),
            (cli.slots, 'describe', iter(())),
            (cli.slots, 'sync', []),
            (cli.oci_iam, 'rotate_seed', 'fingerprint'),
            (cli.oci_iam, 'adopt_domain', 'https://domain.example'),
            (cli.b2, 'rotate_seed', 'key-id'),
            (cli.derived, 'cloudflare_zones', None),
            (cli.derived, 'cloudflare_gateway_acme', 'token-id'),
            (cli.derived, 'oci_physical', 'ocid1.user.test'),
            (cli.derived, 'oci_state_backend', Path('placeholder')),
            (cli.derived, 'b2_management', 'key-id'),
            (cli.derived, 'drill_age_identity', 'age1recipient'),
            (cli.derived, 'drill_credentials', {}),
            (cli.derived, 'b2_freshness_dumps', 'key-id'),
            (cli.devices, 'deliver', ()),
            # Slots are files in the checkout this test is running from, so
            # the writer is stubbed: a dispatch test must not leave a
            # placeholder passphrase where mise would then read it.
            (cli.workstation, 'write', Path('placeholder')),
        )
        for module, attribute, result in handlers:
            name = f'{module.__name__.rsplit(".", 1)[-1]}.{attribute}'
            self.signatures[name] = inspect.signature(getattr(module, attribute))
            monkeypatch.setattr(module, attribute, self.stub(name, result))
        # A vault that opens nothing: what is under test is the dispatch, and
        # a real one would want a real recovery key.
        self.signatures['store.get'] = inspect.signature(KdbxStore.get)
        monkeypatch.setattr(KdbxStore, 'get', self.stub('store.get', 'a-secret'))
        # The underlying function rather than the bound classmethod, because
        # the stub replacing it is bound the same way and so is recorded with
        # `cls` among its arguments.
        self.signatures['escrow.Vault.open'] = inspect.signature(cli.escrow.Vault.__dict__['open'].__func__)
        monkeypatch.setattr(cli.escrow.Vault, 'open', classmethod(self.stub('escrow.Vault.open', _Vault())))
        methods: tuple[tuple[str, Any], ...] = (
            ('entries', []),
            ('describe', {}),
            ('remember', None),
            ('forget', None),
            ('unlock_with', None),
        )
        for attribute, result in methods:
            self.signatures[f'store.{attribute}'] = inspect.signature(getattr(KdbxStore, attribute))
            monkeypatch.setattr(KdbxStore, attribute, self.stub(f'store.{attribute}', result))
        # `kit rotate` creates the successor database, and `kit bootstrap`
        # creates the kit when there is none; neither should write a file here.
        # Opening is recorded too, so that which of the two `rotate`'s
        # destination reaches can be told apart.
        self.signatures['store.create'] = inspect.signature(KdbxStore.create)
        monkeypatch.setattr(KdbxStore, 'create', self.stub('store.create', kit))
        self.signatures['store.from_env'] = inspect.signature(KdbxStore.from_env)
        monkeypatch.setattr(KdbxStore, 'from_env', self.stub('store.from_env', kit))
        monkeypatch.setattr('getpass.getpass', lambda _prompt='': PASSWORD)
        # `derived <row> recover` refuses to print a secret to a terminal,
        # and whether the test runner has one is not this test's business;
        # `derived <row> import` reads its value from standard input.
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
        assert (['seed', member, 'rotate'] in found) == seed.mints_own_successor
        if seed.repair is not None:
            assert ['seed', member, seed.repair.verb] in found
    for member in masters.ROOTS:
        assert ['root', member, 'remember'] in found
    for member in devices.DEVICES:
        assert ['derived', member, 'record'] in found
    for row, label in escrow.rows().items():
        # The verb an escrowed row carries follows from its origin: a value
        # drawn here is generated, one made in a console is recorded, and
        # offering the wrong one would advertise a mint of a credential this
        # side cannot produce.
        assert ['derived', row, label.verb] in found
        assert ['derived', row, 'recover'] in found
    assert ['kit', 'bootstrap'] in found


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
    assert cli.main(['kit', 'bootstrap', '--only', 'recovery']) == 0

    # `bootstrap` sits where a row name sits for the subjects that have rows,
    # and carries its own options there; they have to reach the walk through
    # that shape.
    assert [kwargs['only'] for name, _, kwargs in dispatch.calls if name == 'lifecycle.bootstrap'] == ['recovery']


def test_rotate_carries_its_only_and_its_destination_through(dispatch: Dispatch, tmp_path: Path) -> None:
    successor = tmp_path / 'next.kdbx'

    assert cli.main(['kit', 'rotate', '--into', str(successor), '--only', 'oci']) == 0

    # The destination travels as a path with the two ways of reaching it
    # (`lifecycle.Successor`), which `rotate` chooses between once its own
    # refusals are behind it, so the command itself neither makes nor opens
    # the file.
    assert 'store.create' not in dispatch.reached
    assert [args[0] for name, args, _ in dispatch.calls if name == 'store.from_env'] == [None]
    rotate_calls = [(args, kwargs) for name, args, kwargs in dispatch.calls if name == 'lifecycle.rotate']
    assert [kwargs['only'] for _, kwargs in rotate_calls] == ['oci']
    assert rotate_calls[0][0][1].path == successor


def test_rotate_hands_the_walk_an_opener_and_a_creator_for_its_destination(dispatch: Dispatch, tmp_path: Path) -> None:
    # An existing `--into` is opened the way every other command opens the
    # kit, and an absent one is created with a password asked for it; neither
    # happens until the walk asks, and the walk is what decides which.
    successor = tmp_path / 'next.kdbx'
    assert cli.main(['kit', 'rotate', '--into', str(successor)]) == 0
    destination = next(args[1] for name, args, _ in dispatch.calls if name == 'lifecycle.rotate')

    _ = destination.open(successor)
    _ = destination.create(successor)

    # The one open before the walk's is the kit's own (`_kit`, by `--kdbx`).
    assert [args[0] for name, args, _ in dispatch.calls if name == 'store.from_env'] == [None, successor]
    assert [args[0] for name, args, _ in dispatch.calls if name == 'store.create'] == [successor]


def test_rotate_refuses_an_unknown_member_before_creating_the_successor(
    dispatch: Dispatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The command owns the successor file, not `rotate`, so a `--only` nothing
    # answers has to be refused here or the typo leaves an empty kit behind.
    assert cli.main(['kit', 'rotate', '--into', str(tmp_path / 'next.kdbx'), '--only', 'nonesuch']) == 1

    assert 'no seed named' in caplog.text
    assert 'store.create' not in dispatch.reached
    assert 'lifecycle.rotate' not in dispatch.reached


def test_the_zones_row_is_pushed_into_the_stack_it_names(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'cloudflare-zones', 'mint', '--stack', 'elsewhere']) == 0

    # The slot is built by the dispatch, so the stack it names and the state
    # the push opens have to arrive at the row's own function.
    (_, _, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'derived.cloudflare_zones']
    assert not rest
    assert kwargs['stack'].name == 'elsewhere'
    assert 'lifecycle.environment' in dispatch.reached


def refuses_a_stack_of_its_own(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """`argv` is refused *for naming `--stack`*, rather than merely refused.

    `SystemExit` is argparse's answer to every usage error and to a clean
    `--help` alike, so the exception alone says only that the line did not
    parse. The status is what separates a refusal from a successful exit, and
    the message is the only place the flag itself appears.
    """
    with pytest.raises(SystemExit) as refusal:
        _ = cli.build_parser().parse_args(argv)

    assert refusal.value.code == 2
    assert 'unrecognized arguments: --stack' in capsys.readouterr().err


def test_a_row_named_after_its_consumer_takes_no_stack_of_its_own(capsys: pytest.CaptureFixture[str]) -> None:
    # What these three mint is named after the row -- one IAM user, one B2 key
    # name, one Cloudflare token -- and the mint retires every other credential
    # of that name, so a `--stack` would revoke the real stack's live
    # credential on its way to filling a different stack's slot. The flag is
    # absent rather than documented as dangerous.
    for argv in (
        ['derived', 'oci-physical', 'mint', '--compartment', 'ocid1.compartment.test', '--stack', 'elsewhere'],
        ['derived', 'b2-management', 'mint', '--stack', 'elsewhere'],
        ['derived', 'cloudflare-gateway-acme', 'mint', '--stack', 'elsewhere'],
    ):
        refuses_a_stack_of_its_own(argv, capsys)


def test_the_fixed_rows_are_pushed_into_the_stack_they_are_named_after(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'oci-physical', 'mint', '--compartment', 'ocid1.compartment.test']) == 0
    assert cli.main(['derived', 'b2-management', 'mint']) == 0
    assert cli.main(['derived', 'cloudflare-gateway-acme', 'mint']) == 0

    pushed = [kwargs['stack'].name for name, _, kwargs in dispatch.calls if name.startswith('derived.')]
    assert pushed == [cli.derived.PHYSICAL_STACK] * 3


def test_the_drill_identity_is_aimed_at_the_committed_recipient_file_as_the_admin_token(dispatch: Dispatch) -> None:
    # The file the generator writes is the one the appliance's recipient list
    # reads, and the push authenticates as the token `derived sync` uses --
    # read out of the `github` stack rather than from anything in the shell.
    assert cli.main(['derived', 'drill-age-identity', 'generate']) == 0
    assert cli.main(['derived', 'drill-age-identity', 'generate', '--rotate']) == 0

    calls = [(args, kwargs) for name, args, kwargs in dispatch.calls if name == 'derived.drill_age_identity']
    assert [kwargs['rotate'] for _, kwargs in calls] == [False, True]
    assert {kwargs['recipient_file'] for _, kwargs in calls} == {cli.appliance_config.DRILL_RECIPIENT_FILE}
    assert {args[0].token for args, _ in calls} == {'a-token'}


def test_the_drill_credentials_mint_takes_no_compartment(capsys: pytest.CaptureFixture[str]) -> None:
    """The one OCI mint held to the recorded compartment with no way around it.

    `--compartment` exists for rehearsing a bring-up in a tenancy that is not
    this installation's; the drill compartment is a recorded name in the
    recorded tenancy, and a drill key confined to a compartment named on the
    command line would be a key whose bound no test holds.
    """
    with pytest.raises(SystemExit) as refusal:
        _ = cli.build_parser().parse_args(['derived', 'drill-credentials', 'mint', '--compartment', 'ocid1.x'])

    assert refusal.value.code == 2
    assert 'unrecognized arguments: --compartment' in capsys.readouterr().err


def test_the_drill_credentials_reach_their_mint_with_both_seeds_as_the_admin_token(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'drill-credentials', 'mint']) == 0
    assert cli.main(['derived', 'drill-credentials', 'mint', '--only', 'b2', '--b2-entry', 'seeds/other']) == 0

    # Both halves by default and one on request; each seed from the entry its
    # own flag names; the push authenticates as the token `derived sync` uses,
    # read out of the `github` stack rather than from anything in the shell.
    calls = [(args, kwargs) for name, args, kwargs in dispatch.calls if name == 'derived.drill_credentials']
    assert [kwargs['only'] for _, kwargs in calls] == [None, 'b2']
    assert [kwargs['b2_seed_entry'] for _, kwargs in calls] == [cli.derived.B2_SEED_ENTRY, 'seeds/other']
    assert {kwargs['oci_seed_entry'] for _, kwargs in calls} == {cli.derived.OCI_SEED_ENTRY}
    assert {args[1].token for args, _ in calls} == {'a-token'}
    assert {type(args[0]) for args, _ in calls} == {KdbxStore}


def test_the_freshness_key_reaches_its_mint_with_the_seed_as_the_admin_token(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'b2-freshness-dumps', 'mint']) == 0
    assert cli.main(['derived', 'b2-freshness-dumps', 'mint', '--entry', 'seeds/other']) == 0

    # The seed from the entry `--entry` names; the push authenticates as the
    # token `derived sync` uses, read out of the `github` stack rather than
    # from anything in the shell.
    calls = [(args, kwargs) for name, args, kwargs in dispatch.calls if name == 'derived.b2_freshness_dumps']
    assert [kwargs['seed_entry'] for _, kwargs in calls] == [cli.derived.B2_SEED_ENTRY, 'seeds/other']
    assert {args[1].token for args, _ in calls} == {'a-token'}
    assert {type(args[0]) for args, _ in calls} == {KdbxStore}


def test_a_half_the_drill_does_not_have_is_refused_by_the_parser(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refusal:
        _ = cli.build_parser().parse_args(['derived', 'drill-credentials', 'mint', '--only', 'cloudflare'])

    assert refusal.value.code == 2
    assert 'invalid choice' in capsys.readouterr().err


def test_the_compartment_reaches_the_row_that_confines_the_key_with_it(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'oci-physical', 'mint', '--compartment', 'ocid1.compartment.test']) == 0

    # The policy the mint writes names this compartment, so a command that
    # dropped it would mint a key confined to somewhere else entirely.
    (_, _, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'derived.oci_physical']
    assert not rest
    assert kwargs['compartment_id'] == 'ocid1.compartment.test'


def test_a_mint_with_no_compartment_leaves_the_choice_to_conventions(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'oci-physical', 'mint']) == 0
    assert cli.main(['derived', 'oci-state-backend', 'mint']) == 0

    # The ordinary bring-up names no compartment at all: the mapping does, and
    # the row's own function creates what is not there. `None` is how the
    # command says "the convention", rather than a second copy of it here.
    passed = [kwargs['compartment_id'] for name, _, kwargs in dispatch.calls if name.startswith('derived.oci')]
    assert passed == [None, None]


def test_a_device_row_is_pushed_into_the_stack_its_table_names(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'adguard', 'record']) == 0

    # The row decides the stack, and the push still needs the state backend
    # open: a device credential is a config secret like any other.
    (_, args, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'devices.deliver']
    assert not rest
    assert args[0] is devices.DEVICES['adguard']
    assert kwargs['stack'].name == devices.DEVICES['adguard'].stack
    assert 'lifecycle.environment' in dispatch.reached


def test_a_device_row_takes_no_stack_of_its_own(capsys: pytest.CaptureFixture[str]) -> None:
    # The credential authenticates against one device, and one stack talks to
    # that device; a `--stack` would deliver it where nothing checks it.
    refuses_a_stack_of_its_own(['derived', 'unifi', 'record', '--stack', 'elsewhere'], capsys)


def test_what_a_device_run_is_handed_reaches_the_delivery(dispatch: Dispatch, tmp_path: Path) -> None:
    username = tmp_path / 'username'
    password = tmp_path / 'password'
    _ = username.write_text('an-admin\n')
    _ = password.write_text('a-password\n')

    argv = ['derived', 'adguard', 'record', '--username-file', str(username), '--password-file', str(password)]
    assert cli.main(argv) == 0

    # A secret is named as a file rather than as a value, and each one arrives
    # addressed by the field name the table gives it: a row with two of them
    # is exactly where a mix-up would otherwise go unnoticed.
    (_, _, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'devices.deliver']
    assert not rest
    assert kwargs['given'] == {'username': str(username), 'password': str(password)}


def test_the_controller_row_records_the_key_and_no_address(dispatch: Dispatch, tmp_path: Path) -> None:
    key = tmp_path / 'api-key'
    _ = key.write_text('an-api-key\n')

    assert cli.main(['derived', 'unifi', 'record', '--api-key-file', str(key)]) == 0

    (_, _, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'devices.deliver']
    assert not rest
    assert kwargs['given'] == {'api-key': str(key)}

    # The controller answers at the overlay address this program's own
    # ZeroTier roster assigns the gateway, which the consuming stack derives.
    # There is therefore nothing to record it with -- an option to would be
    # inviting a second copy of a stated constant.
    with pytest.raises(SystemExit):
        _ = cli.main(['derived', 'unifi', 'record', '--api-key-file', str(key), '--api-url', 'https://198.51.100.1'])


def test_the_passphrase_is_written_to_its_slot_rather_than_redirected(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'pulumi-passphrase', 'recover']) == 0

    # The command owns the file, so it is 0600 from the moment it exists
    # instead of whatever the shell's umask happened to be.
    (_, args, _), *rest = [call for call in dispatch.calls if call[0] == 'workstation.write']
    assert not rest
    assert args[0] == cli.workstation.passphrase_path()
    assert args[1] == 'a-secret'


def test_generating_the_passphrase_also_fills_its_slot(dispatch: Dispatch) -> None:
    # generate -> escrow -> push: the value reaches the slot mise.toml reads
    # in the same run, so a rotation is one command rather than two.
    assert cli.main(['derived', 'pulumi-passphrase', 'generate']) == 0

    (_, args, _), *rest = [call for call in dispatch.calls if call[0] == 'workstation.write']
    assert not rest
    assert args[0] == cli.workstation.passphrase_path()
    assert args[1] == 'a-secret'


def test_generating_a_label_with_no_slot_writes_no_file(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'state-backend-ca', 'generate']) == 0

    assert 'workstation.write' not in dispatch.reached


def test_the_passphrase_can_still_be_piped_to_another_machine(dispatch: Dispatch) -> None:
    assert cli.main(['derived', 'pulumi-passphrase', 'recover', '--stdout']) == 0

    assert 'workstation.write' not in dispatch.reached


def test_import_refuses_an_empty_pipe_and_says_where_it_came_from(
    dispatch: Dispatch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The failure this exists for: `producer | credentials derived <row> import`
    # where the producer died. Import still gets a value, and escrowing it
    # files an empty string as a generation nobody notices until a recovery.
    monkeypatch.setattr('sys.stdin', io.StringIO('\n'))

    assert cli.main(['derived', 'pulumi-passphrase', 'import']) == 1

    assert 'standard input was empty' in caplog.text
    assert 'escrow.adopt' not in dispatch.reached


def test_import_refuses_an_empty_slot(
    dispatch: Dispatch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / 'passphrase'
    _ = empty.write_text('')

    def slot(_label: str) -> cli.escrow.WorkstationSlot:
        return cli.escrow.WorkstationSlot(path=lambda: empty, read_by='a test reads it')

    monkeypatch.setattr(cli.escrow, 'slot', slot)

    assert cli.main(['derived', 'pulumi-passphrase', 'import', '--from-slot']) == 1

    assert str(empty) in caplog.text
    assert 'escrow.adopt' not in dispatch.reached


def test_check_runs_without_opening_a_kit(dispatch: Dispatch) -> None:
    # The one command a stranger with a clone can run. Opening the kit here
    # would make a check into a ceremony.
    assert cli.main(['derived', 'check']) == 0

    assert 'escrow.check' in dispatch.reached
    assert not [name for name in dispatch.reached if name.startswith('store.')]


def test_seed_create_dispatches_the_row_the_member_names(dispatch: Dispatch) -> None:
    assert cli.main(['seed', 'oci', 'create']) == 0

    # One row at a time goes through the same entrypoint `kit bootstrap` walks,
    # with the register's own row rather than a name looked up again inside.
    (_, args, kwargs), *rest = [call for call in dispatch.calls if call[0] == 'lifecycle.create_seed']
    assert not rest
    assert args[0] is entries.SEEDS['oci']
    assert kwargs['entry'] == entries.SEEDS['oci'].entry


def test_every_command_that_opens_an_escrow_opens_the_one_it_was_pointed_at(
    dispatch: Dispatch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--escrow` sits in front of every subject, so it has to reach all of them.

    A census over the parser's leaves rather than over a list of the commands
    that open one: a command that opens the escrow without carrying the
    registry there reads the checkout's own `escrow/` while the operator is
    pointed at a second tree, silently and only for that command. The walk is
    what makes a subcommand written later hold to this without anyone editing
    this file.
    """
    elsewhere = tmp_path / 'second-tree' / 'escrow'
    pointed_at = escrow.Registry(root=elsewhere)

    opened: list[tuple[str, str, Any]] = []
    for argv in commands():
        # Each leaf gets the pipe of its own that the parametrized walk gets
        # from its fixture: a row whose value is read from standard input
        # reaches nothing once an earlier command has drained it.
        monkeypatch.setattr('sys.stdin', io.StringIO('a-value'))
        seen = len(dispatch.calls)
        _ = cli.main(['--escrow', str(elsewhere), *argv])
        opened.extend((' '.join(argv), name, registry) for name, registry in dispatch.registries(dispatch.calls[seen:]))

    # A census that reached no such handler at all would hold nothing while
    # still passing, which is the one way this stops being a property.
    assert opened
    assert opened == [(command, name, pointed_at) for command, name, _ in opened]
