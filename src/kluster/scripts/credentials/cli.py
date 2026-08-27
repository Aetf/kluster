"""`credentials` — the executable form of the credential register.

The command tree is the register's own tiers (docs/credentials.md), not the
accounts behind them, and every command reads `credentials <subject> [<row>]
<verb>`. There are four subjects: `root` for the account roots a workstation
borrows, `seed` for §2's rows, `kit` for the offline store and what is done to
the whole of it, and `derived` for §3's rows. A row carries one name across
the tree, the slot map and the register's tables.

Reading `credentials --help` beside the register should show the same shapes;
a command with no row, or a row with no command, is the bug that discipline is
meant to surface.

Every action is mint -> push to every slot -> verify, and therefore idempotent:
rotation is a re-run, not a second procedure.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import (
    b2,
    derived,
    devices,
    entries,
    escrow,
    github_secrets,
    lifecycle,
    masters,
    oci_iam,
    pulumi_config,
    slots,
    workstation,
)
from .age import AgeError
from .escrow import EscrowError
from .kdbx import PATH_ENV, KdbxError, KdbxStore, default_path
from .masters import CredentialRejected
from .pulumi_config import SlotRefused

log = logging.getLogger(__name__)

#: Which command runs when. The tree says what exists; this says what to do
#: with it, because "one subcommand per register row" answers neither "where
#: do I start" nor "is this the one that destroys something".
_ORDER = """when to run what (docs/credentials.md §4):

  bring-up, from nothing
    0. credentials root <root> remember
         Keeps an account root (§2) on this machine, once, so the mints
         below ask for nothing. Every root is looked up the same way:
         desktop secret store, then its token file, then its environment
         variable, then a prompt -- so skipping this costs a prompt rather
         than a failure, which is how a headless run works.
    1. credentials kit bootstrap
         Fills a kit with every seed in §2, creating the kit if it is absent.
         The recovery key is one of them, and creating it also writes
         escrow/RECIPIENTS. Stops at each credential no API can create and
         prints the console steps. Re-run it to resume.
    2. credentials derived oci-state-backend mint --compartment <ocid>
         The appliance's own OCI key, minted from the seed into a
         workstation slot. The next step reads it there; nothing else does.
    3. state-backend provision
         The Pulumi state backend, which every stack needs before it can act,
         and the first thing to escrow: it generates the CA and the backup
         identities it is about to install and commits their ciphertexts.
    4. credentials derived pulumi-passphrase generate
         The one escrowed row no other command mints. It writes the
         workstation slot as well as the ciphertext, so mise.toml puts the
         passphrase in the environment of every pulumi run from here on.
    5. credentials derived cloudflare-zones mint
         Mints the zone-scoped Cloudflare token from the seed and writes
         it into the dns stack's config, which is then committed. One
         §3 row per command; re-running one rotates it.
    6. credentials derived oci-physical mint --compartment <ocid>
       credentials derived b2-management mint
         The two provider credentials the physical stack runs on, into its
         config, which is then committed like the one above.
    7. credentials derived unifi record
       credentials derived adguard record
         The two §3 credentials no API here mints: each is made on the
         device that checks it, so the command prints the steps that
         create it, takes the value without echoing it, and writes it
         into the stack config that reads it -- physical for the UniFi
         key, dns for the AdGuard login. Both files are then committed.
    8. credentials derived sync
         The GitHub secrets CI reads, for the §3 rows whose value lives
         somewhere else and is copied into a slot. Run it again whenever one
         of those values moves; a row it cannot fill yet says which slot is
         waiting on what.

  on a workstation that develops without the kit
    Copy the .credentials directory from a machine that has one: the
    passphrase slot and the client bundle come with it. On a machine that
    does hold the kit, `credentials derived pulumi-passphrase recover`
    writes that slot once, and mise.toml reads it on every pulumi run.

  day to day
    Nothing. No runtime credential is in the kit, so no operation outside
    bring-up, rotation and the yearly offline day opens it (§2.1).
    `credentials derived check` needs no kit at all.

  when one seed is lost
    credentials kit bootstrap --only <member>
         Re-creates that row alone; the rest of the kit is untouched. Not
         the recovery key: every ciphertext under escrow/ opens with that
         one and nothing else, so losing it is losing them.

  one-time repair
    credentials seed oci domain
         Records the tenancy's identity domain on an OCI row written before
         that attribute existed. Without it a rotation mints a successor and
         cannot retire what it supersedes, because the legacy delete call is
         refused. Borrows the account root, once.

  rotating one credential (§4.2)
    credentials derived <row> generate
         A new generation for that escrowed row alone, adopted by re-running
         what consumes it. Nothing else moves.

  rotating the kit (§4.2)
    credentials kit rotate --into <new kit>
         Writes a *new* database, and re-wraps every escrow ciphertext to
         the successor recovery key. No production secret changes value, so
         the retired file is destroyable once the run is verified.

  looking without changing
    credentials derived check     every expected escrow row present, every
                                  ciphertext an age file, generations dense
    credentials derived ls        every §3 credential, where its value comes
                                  from, and every slot it lands in
    credentials kit ls | show <entry>
    credentials root ls           which account roots this machine holds, and
                                  which layer of the chain each comes from
    credentials kit password remember
                                  stores the kit's master password in the
                                  desktop secret store, so a long run is not
                                  guarded by a password typed into it
"""


def _slot_source(command: argparse.ArgumentParser) -> None:
    """Say where the state backend's client bundle is.

    Every §3 row delivered into a Pulumi config secret needs it: the stack's
    configuration lives in the state backend, so pushing into it means
    reaching the backend, which is this bundle plus the passphrase the kit
    recovers.
    """
    _ = command.add_argument(
        '--bundle-dir',
        type=Path,
        default=workstation.bundle_dir(),
        help='where `state-backend` wrote the client bundle',
    )


def _oci_consumer(command: argparse.ArgumentParser) -> None:
    """The two arguments every §3 OCI row is minted with.

    The seed the mint reads and the compartment it confines the result to are
    the same question for each consumer; where the rows differ is where the
    answer is delivered, which is what makes them separate subcommands.
    """
    _ = command.add_argument(
        '--entry', default=derived.OCI_SEED_ENTRY, help=f'the seed row (default: {derived.OCI_SEED_ENTRY})'
    )
    # Required because nothing in this repository knows it: a compartment OCID
    # is a fact about the tenancy, and it is what the minted policy confines
    # the key to.
    _ = command.add_argument(
        '--compartment', required=True, metavar='<ocid>', help='the compartment the key may administer'
    )


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree, as data.

    Public because the tree is generated rather than written out: a test walks
    it and drives every leaf through `main`, so a register row that `main`
    cannot dispatch fails there rather than on an operator's first run.
    """
    parser = argparse.ArgumentParser(
        prog='credentials',
        description=__doc__,
        epilog=_ORDER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument(
        '--kdbx',
        type=Path,
        default=None,
        help=f'the seed kit (default: ${PATH_ENV}, else {workstation.kit_path()})',
    )
    _ = parser.add_argument(
        '--escrow',
        type=Path,
        default=None,
        help=f'the escrow registry (default: the {escrow.DIRECTORY}/ directory of this checkout)',
    )
    subjects = parser.add_subparsers(dest='subject', required=True, metavar='<subject>')

    # The account roots (§2) are not in the kit and not in any database this
    # repository opens: each is looked up through one chain -- desktop secret
    # store, token file, environment variable, prompt (`masters.py`). This
    # subject is how they get onto a machine, and the only thing that writes
    # them.
    root_subject = subjects.add_parser('root', help='the account roots the workstation borrows (§2)')
    roots = root_subject.add_subparsers(dest='member', required=True, metavar='<root>')
    root_listing = roots.add_parser('ls', help='which roots this machine holds, and where; prints no values')
    root_listing.set_defaults(action='ls')
    for account in masters.ROOTS.values():
        root_cmd = roots.add_parser(account.member, help=account.title)
        root_verbs = root_cmd.add_subparsers(dest='action', required=True, metavar='<verb>')
        _ = root_verbs.add_parser('remember', help='prompt for it and keep it on this machine')
        _ = root_verbs.add_parser('forget', help='remove it from the secret store and from its token file')

    # The tree is generated from §2's table rather than written out, so a
    # seed that exists in the register and nowhere in the code shows up as a
    # subcommand that refuses to run -- not as a subcommand that is missing.
    seed_subject = subjects.add_parser('seed', help='the seed layer (§2), one member per row')
    members = seed_subject.add_subparsers(dest='member', required=True, metavar='<member>')
    for seed in entries.SEEDS.values():
        member = members.add_parser(seed.member, help=f'mints {seed.mints}')
        _ = member.add_argument('--entry', default=seed.entry, help=f'entry holding it (default: {seed.entry})')
        seed_verbs = member.add_subparsers(dest='action', required=True, metavar='<verb>')
        _ = seed_verbs.add_parser(
            'create',
            help='generate it (bring-up, once)'
            if seed.member == 'recovery'
            else 'mint it from the account root (bring-up, or seed loss)',
        )
        if seed.self_reproducing:
            _ = seed_verbs.add_parser('rotate', help='have the seed mint and install its successor')
        if seed.repair is not None:
            _ = seed_verbs.add_parser(seed.repair[0], help=seed.repair[1])

    # The offline store itself, and the things done to the whole of it. The two
    # walks take §2's table in order and skip what is already there, so an
    # interrupted run is resumed by re-running it rather than by remembering
    # where it stopped.
    kit_subject = subjects.add_parser('kit', help='the offline store, and what is done to the whole of it (§2.1)')
    kit_verbs = kit_subject.add_subparsers(dest='member', required=True, metavar='<verb>')

    boot = kit_verbs.add_parser(
        'bootstrap',
        help='fill a kit with every seed (§4.1); creates the kit if absent',
        description=(
            'Create every seed the kit does not hold yet, and the kit itself if there is none. A row a platform '
            'can mint is minted; the rest stop and print the console steps that create them, so an interrupted '
            'run is resumed by running this again. Creating the recovery row also writes the public half of the '
            'recovery key to escrow/RECIPIENTS, which is a file to commit.'
        ),
    )
    boot.set_defaults(action='bootstrap')
    _ = boot.add_argument(
        '--only',
        default=None,
        metavar='<member>',
        help='create just this seed; `--only recovery` is how a kit that predates the escrow gets its key',
    )

    rot = kit_verbs.add_parser('rotate', help='write a new kit in which every seed is replaced (§4.2)')
    rot.set_defaults(action='rotate')
    _ = rot.add_argument('--into', type=Path, required=True, help='path for the successor kit; must not exist')
    _ = rot.add_argument('--only', default=None, metavar='<member>', help='rotate just this seed')

    rewrap = kit_verbs.add_parser(
        'rewrap',
        help='re-encrypt every escrowed ciphertext to the recipients now on file',
        description=(
            'Open every ciphertext under escrow/ with the recovery key this kit holds, and write each back '
            'encrypted to whatever escrow/RECIPIENTS already names. No plaintext changes, so no consumer is '
            'touched. `kit rotate` does this for itself; running it alone finishes a rotation that died part '
            'way through, or takes in a ciphertext written while the file already named the successor. A run '
            'that would leave the registry with nothing in hand able to open it is refused.'
        ),
    )
    rewrap.set_defaults(action='rewrap')

    kit_listing = kit_verbs.add_parser('ls', help='list entry paths')
    kit_listing.set_defaults(action='ls')
    _ = kit_listing.add_argument('group', nargs='?', default='/')
    show = kit_verbs.add_parser('show', help="an entry's non-secret attributes")
    show.set_defaults(action='show')
    _ = show.add_argument('entry')

    # The kit is the one database a run opens, and a bring-up holds it open
    # for minutes; guarding that with a password typed into an unwatched
    # process is what this avoids.
    password = kit_verbs.add_parser('password', help="this database's master password, as this machine holds it")
    password_verbs = password.add_subparsers(dest='action', required=True, metavar='<verb>')
    _ = password_verbs.add_parser('remember', help="store this database's master password in the desktop secret store")
    _ = password_verbs.add_parser('forget', help='remove it from the secret store')

    # The other half of the register: §3's rows. Each is named here exactly as
    # the slot map names it (`slots.py`), so a row has one spelling across the
    # tree, the map and the register's tables. What differs between rows is
    # the verb, because what differs between them is how the value comes into
    # being: `mint` for a row a seed mints, `generate`/`import`/`recover` for
    # a row generated here and escrowed (§2.2), `record` for a row made on a
    # device of the estate and typed in.
    #
    # A row joins the tree when its consumer exists -- a mint with nowhere to
    # deliver would park a secret, which is the one thing the register forbids
    # outright.
    derived_subject = subjects.add_parser('derived', help='the §3 credentials, one row each, and the map of them (§4)')
    rows = derived_subject.add_subparsers(dest='member', required=True, metavar='<row>')

    # Three verbs about the whole of §3 rather than about one row of it. The
    # map is §3's machine-readable half, and all three read it.
    derived_listing = rows.add_parser('ls', help='the whole slot map; needs no kit, no token and no network')
    derived_listing.set_defaults(action='ls')
    checking = rows.add_parser('check', help='what the escrow is missing or holds malformed; needs no kit')
    checking.set_defaults(action='check')
    syncing = rows.add_parser(
        'sync',
        help='copy into their GitHub slots the rows whose value lives elsewhere',
        description=(
            'Fill the GitHub secrets of the rows whose value is a copy of something that lives somewhere else: '
            'a value generated inside a stack and read back out of its state, and a value that is typed in '
            'because this slot is the only place it is stored. Resolve, push, verify, per row, so a first fill '
            'and a refill after a channel is lost are one command. '
            'A row born into its slot is out of scope: a minted credential is disclosed once, to the call that '
            'creates it, so its own `mint` fills its slot in the same run and asking again would produce a '
            'different credential. Naming one is refused rather than silently doing nothing.'
        ),
    )
    syncing.set_defaults(action='sync')
    _ = syncing.add_argument(
        '--only', default=None, metavar='<row>', help='one row of the map; `derived ls` names them'
    )
    _slot_source(syncing)

    # In bring-up order (§4.1), which is the order an operator meets them in.
    #
    # Only the zones row takes a `--stack`. What each of the others mints is
    # named after the row and the mint retires everything else of that name, so
    # a delivery aimed elsewhere would revoke the real stack's live credential
    # on its way to filling another stack's slot (`derived.py`).
    appliance_key = rows.add_parser(derived.OCI_STATE_BACKEND_ROW, help="the appliance provisioner's own OCI key (§3)")
    appliance_verbs = appliance_key.add_subparsers(dest='action', required=True, metavar='<verb>')
    appliance_mint = appliance_verbs.add_parser(
        'mint', help='mint the user, group, policy and key from the seed into the workstation slot (§4.4)'
    )
    _oci_consumer(appliance_mint)

    zones_row = rows.add_parser(derived.ZONES_ROW, help='the zone-scoped Cloudflare provider token (§3)')
    zones_verbs = zones_row.add_subparsers(dest='action', required=True, metavar='<verb>')
    zones_mint = zones_verbs.add_parser(
        'mint', help="mint it from the seed into the dns stack's config secret, with the account id beside it"
    )
    _ = zones_mint.add_argument(
        '--entry',
        default=derived.CLOUDFLARE_SEED_ENTRY,
        help=f'the seed row (default: {derived.CLOUDFLARE_SEED_ENTRY})',
    )
    _ = zones_mint.add_argument(
        '--stack',
        default=derived.ZONES_STACK,
        help=f'the stack whose config takes the token (default: {derived.ZONES_STACK})',
    )
    _slot_source(zones_mint)

    physical_key = rows.add_parser(derived.OCI_PHYSICAL_ROW, help="the physical stack's OCI user and API key (§3)")
    physical_verbs = physical_key.add_subparsers(dest='action', required=True, metavar='<verb>')
    physical_mint = physical_verbs.add_parser(
        'mint', help="mint the user, group, policy and key from the seed into that stack's config secrets"
    )
    _oci_consumer(physical_mint)
    _slot_source(physical_mint)

    management_key = rows.add_parser(derived.B2_MANAGEMENT_ROW, help='the B2 bucket and lifecycle admin key (§3)')
    management_verbs = management_key.add_subparsers(dest='action', required=True, metavar='<verb>')
    management_mint = management_verbs.add_parser(
        'mint', help="mint it from the seed into the physical stack's config secret"
    )
    _ = management_mint.add_argument(
        '--entry', default=derived.B2_SEED_ENTRY, help=f'the seed row (default: {derived.B2_SEED_ENTRY})'
    )
    _slot_source(management_mint)

    # §3's rows whose credential is made on a device of the estate rather than
    # minted from a seed. `record` prints the console steps that create it,
    # takes the value without echoing it and pushes it into the stack that
    # reads it (`devices.py`); nothing here mints anything, so the delivery is
    # the whole of the act.
    for device in devices.DEVICES.values():
        device_row = rows.add_parser(device.member, help=f'{device.title} (§3)')
        device_verbs = device_row.add_subparsers(dest='action', required=True, metavar='<verb>')
        record = device_verbs.add_parser(
            'record', help=f"take it from the console into the {device.stack} stack's config"
        )
        for field in device.fields:
            _ = record.add_argument(
                field.flag,
                default=None,
                metavar='<path>' if field.secret else '<value>',
                help=(
                    f'read {field.describes} from a file rather than a prompt (`{devices.STDIN}` reads stdin)'
                    if field.secret
                    else f'{field.describes}, rather than a prompt'
                ),
            )
        _slot_source(record)

    # §3's escrowed rows (§2.2): random secrets whose ciphertexts are
    # committed, opened by the one recovery key the kit holds. Generating and
    # escrowing are a single act, so no verb here hands out a secret the
    # registry does not carry. The row's escrow label travels on the namespace
    # rather than as an argument -- the directory keeps its `/` paths, and the
    # tree names the row the way the rest of §3 is named (`escrow.row_name`).
    for name, label in escrow.rows().items():
        escrowed = rows.add_parser(name, help=f'{label.what} (§2.2)')
        escrowed.set_defaults(label=label.name)
        escrow_verbs = escrowed.add_subparsers(dest='action', required=True, metavar='<verb>')
        _ = escrow_verbs.add_parser('generate', help="mint this row's next generation and escrow it")
        adopt = escrow_verbs.add_parser(
            'import', help="escrow a value that already exists as this row's next generation"
        )
        _ = adopt.add_argument(
            '--from-slot',
            action='store_true',
            help='read it from its workstation slot instead of standard input',
        )
        recover = escrow_verbs.add_parser('recover', help='open the escrowed secret with the kit')
        _ = recover.add_argument('--generation', type=int, default=None, help='default: the newest')
        _ = recover.add_argument('--stdout', action='store_true', help='print it even though it has a slot')

    return parser


def _layers(held: dict[str, str | None]) -> str:
    """One root's line in `root ls`: where its fields are, never what they are.

    A root whose fields all come from the same layer is one phrase, because
    that is the ordinary case and a field-by-field listing would bury it. The
    mixed case is worth spelling out — a field answered by the environment
    while its siblings sit in the store is usually a shell that will not be
    there next time.
    """
    missing = [name for name, layer in held.items() if layer is None]
    if missing:
        return 'missing: ' + ', '.join(missing)
    layers = {layer for layer in held.values() if layer is not None}
    if len(layers) == 1:
        return f'in {layers.pop()}'
    return ', '.join(f'{name} in {layer}' for name, layer in held.items() if layer is not None)


def _root(args: argparse.Namespace) -> int:
    """The account-root commands, which need no kit and open no database."""
    if args.action == 'ls':
        for account in masters.ROOTS.values():
            print(f'{account.member}: {_layers(masters.stored(account))}')
        return 0
    account = masters.ROOTS[args.member]
    if args.action == 'remember':
        log.info('remembered %s for %s', ', '.join(masters.remember(account, input)), account.member)
    else:
        masters.forget(account)
    return 0


def _kit(args: argparse.Namespace) -> KdbxStore:
    """The seed kit, created on the spot if `kit bootstrap` is starting from none.

    Every other command needs it to exist already: creating one by accident,
    because a path was mistyped, would look like an empty kit rather than a
    missing one.
    """
    path = args.kdbx or default_path()
    if (args.subject, args.member) == ('kit', 'bootstrap') and not path.exists():
        log.info('no kit at %s; creating it', path)
        return KdbxStore.create(path, getpass.getpass(f'new master password for {path.name}: '))
    return KdbxStore.from_env(args.kdbx)


def _stack(args: argparse.Namespace, store: KdbxStore, name: str) -> pulumi_config.Stack:
    """The config slot a §3 row is pushed into.

    Opened with the same two variables a `pulumi` run needs, recovered with the
    kit that is already open rather than expected in the environment: one
    command is one credential delivered, not a shell that has to be prepared
    first.

    The stack is named by the caller rather than read off `args`, because only
    one row has a stack to choose: the zones token is scoped to zones and can
    be delivered anywhere, while a row whose credential is named after its
    consumer can only be delivered to that consumer (`derived`).
    """
    return pulumi_config.Stack(
        name=name,
        directory=pulumi_config.project_dir(),
        env=lifecycle.environment(store, args.bundle_dir),
    )


def _slots(args: argparse.Namespace, store: KdbxStore, registry: escrow.Registry) -> slots.Context:
    """What `derived sync` may reach for, with everything slow left unopened.

    The token is fetched up front because every push needs it and the chain
    that finds it may have to ask (`masters.py`); the kit's escrow and the state
    backend are passed as openers, so pushing the one typed-in row asks for
    neither and a row recovered from escrow never reaches for a backend.
    """
    return slots.Context(
        forge=github_secrets.Forge(token=lifecycle.root('github', input)['token']),
        open_vault=lambda: escrow.Vault.open(store, registry),
        open_environment=lambda: lifecycle.environment(store, args.bundle_dir, registry),
    )


def _check(registry: escrow.Registry) -> int:
    """`derived check`, which is the one command that opens nothing.

    Every problem is printed rather than the first one: a registry is checked
    to learn what is wrong with it, and stopping at the first missing label
    would turn one look into several.
    """
    problems = escrow.check(registry)
    for problem in problems:
        log.error('%s', problem)
    if not problems:
        log.info('escrow: %d label(s) present, every ciphertext an age file', len(escrow.register()))
    return 1 if problems else 0


def _imported(args: argparse.Namespace) -> str:
    """The value `derived <row> import` is to escrow: a slot, or standard input.

    Standard input rather than an argument so the value is never in an argv
    another process can read; the slot is there because the passphrase is
    already sitting in one on the workstation that is doing the migration.

    Empty is refused here, where the source is still known, so the error can
    name it: a command substitution or a pipe whose producer failed hands this
    an empty string, and the traceback that says why has already scrolled past
    by the time the import logs what it escrowed.
    """
    if args.from_slot:
        slot = escrow.SLOTS.get(args.label)
        if slot is None:
            raise EscrowError(f'{args.label} has no workstation slot; pipe the value in instead')
        path = slot()
        value = path.read_text().strip()
        if not value:
            raise EscrowError(f'{path} is empty, so there is nothing to escrow as {args.label}')
        return value
    if sys.stdin.isatty():
        log.info('reading the value for %s from standard input; end it with ctrl-d', args.label)
    value = sys.stdin.read().strip()
    if not value:
        raise EscrowError(
            f'standard input was empty, so there is nothing to escrow as {args.label}; '
            'a producer that failed writes no output, so run it on its own and check what it prints '
            'before piping it in again'
        )
    return value


def _pushed(value: str, *, label: str) -> str:
    """Put a freshly generated secret in the workstation slot its row names.

    Only the passphrase has one; the rest reach their consumers through a
    provisioning run or a seal. A label with no slot is not an error — it is
    the ordinary case.
    """
    slot = escrow.SLOTS.get(label)
    if slot is not None:
        _ = workstation.write(slot(), value)
    return value


def _recovered(args: argparse.Namespace, vault: escrow.Vault) -> int:
    """Put one recovered secret where the operator asked for it.

    A label with a workstation slot goes there, so the ordinary path writes a
    `0600` file the command owns rather than something a shell redirect
    created with whatever umask was in force. Everything else is printed, and
    printing is refused when stdout is the terminal: a secret in the
    scrollback is a secret in the next screen-share.
    """
    value = vault.recover(args.label, args.generation)
    slot = escrow.SLOTS.get(args.label)
    if slot is not None and not args.stdout:
        _ = workstation.write(slot(), value)
        log.info('mise.toml reads it from there on every pulumi run')
        return 0
    if sys.stdout.isatty():
        log.error('this prints a secret; pipe it somewhere')
        return 1
    print(value)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)

    try:
        if args.subject == 'root':
            return _root(args)
        # Ordered before the registry and the kit, like `derived check` below:
        # the map is a checked-in file, so reading it is something a clone can
        # do with no credential of any kind on the machine.
        if (args.subject, args.member) == ('derived', 'ls'):
            for line in slots.describe():
                print(line)
            return 0
        registry = escrow.Registry.open(args.escrow)
        # Ordered before the kit deliberately: the point of `check` is that a
        # clone with no offline database can still say whether the registry is
        # whole.
        if (args.subject, args.member) == ('derived', 'check'):
            return _check(registry)
        store = _kit(args)

        match (args.subject, args.member, args.action):
            case ('kit', 'ls', _):
                for entry in store.entries(args.group):
                    print(entry)
            case ('kit', 'show', _):
                for name, value in store.describe(args.entry).items():
                    print(f'{name}: {value}')
            case ('kit', 'password', 'remember'):
                # Prove it opens the database before storing it: a remembered
                # password that does not work is worse than none.
                password = getpass.getpass(f'master password for {store.path.name}: ')
                store.unlock_with(password)
                store.remember(password)
            case ('kit', 'password', 'forget'):
                store.forget()
            case ('kit', 'bootstrap', _):
                created = lifecycle.bootstrap(store, prompt=input, only=args.only, registry=registry)
                log.info('created %s', ', '.join(created) if created else 'nothing; the kit was already complete')
                # The recovery row is the one whose creation leaves something
                # outside the kit: the recipients file, and a registry that now
                # has somewhere to put ciphertexts but none of them yet.
                if 'recovery' in created:
                    log.info('the recovery key is in the kit; commit %s', registry.recipients_file)
                    for label in escrow.missing(registry):
                        log.warning(
                            'nothing escrowed for %s yet: credentials derived %s generate',
                            label,
                            escrow.row_name(label),
                        )
            case ('kit', 'rotate', _):
                # Before the successor exists, not once `rotate` reaches its
                # walk: a `--only` that names no row would otherwise leave a
                # new database file behind with nothing rotated into it.
                lifecycle.require_member(args.only)
                successor = KdbxStore.create(args.into, getpass.getpass(f'master password for {args.into.name}: '))
                rotated = lifecycle.rotate(store, successor, prompt=input, only=args.only, registry=registry)
                log.info('rotated %s into %s', ', '.join(rotated), args.into)
                if 'recovery' in rotated:
                    log.warning('commit %s: every ciphertext now opens with the successor key alone', registry.root)
            case ('kit', 'rewrap', _):
                _ = escrow.rewrap(registry, identities=[store.get(escrow.RECOVERY_ENTRY)])
            # One row at a time, through the same dispatch `kit bootstrap`
            # walks: a single-row repair and a whole-kit fill must not be able
            # to write a row two different ways.
            case ('seed', member, 'create') if member in entries.SEEDS:
                lifecycle.create_seed(
                    entries.SEEDS[member], kit=store, prompt=input, entry=args.entry, registry=registry
                )
            case ('seed', 'oci', 'rotate'):
                _ = oci_iam.rotate_seed(store, seed_entry=args.entry)
            # The one-time repair for a kit written before the OCI row
            # carried its identity domain: reading the tenancy's domains is
            # the account root's call, so this is the only seed command that
            # borrows one.
            case ('seed', 'oci', 'domain'):
                _ = oci_iam.adopt_domain(store, seed_entry=args.entry, root=lifecycle.root('oci', input))
            case ('seed', 'b2', 'rotate'):
                _ = b2.rotate_seed(store, seed_entry=args.entry)
            # §3's minted rows, one command per row (`_stack` is the slot most
            # of them are pushed into).
            case ('derived', derived.ZONES_ROW, 'mint'):
                _ = derived.cloudflare_zones(store, stack=_stack(args, store, args.stack), seed_entry=args.entry)
            case ('derived', derived.OCI_PHYSICAL_ROW, 'mint'):
                _ = derived.oci_physical(
                    store,
                    stack=_stack(args, store, derived.PHYSICAL_STACK),
                    compartment_id=args.compartment,
                    seed_entry=args.entry,
                )
            # The one §3 row that opens no stack: its consumer is what builds
            # the backend a stack's configuration lives in, so the push is a
            # workstation slot and this command needs no `pulumi` at all.
            case ('derived', derived.OCI_STATE_BACKEND_ROW, 'mint'):
                _ = derived.oci_state_backend(store, compartment_id=args.compartment, seed_entry=args.entry)
            case ('derived', derived.B2_MANAGEMENT_ROW, 'mint'):
                _ = derived.b2_management(
                    store, stack=_stack(args, store, derived.PHYSICAL_STACK), seed_entry=args.entry
                )
            # §3's device rows: no mint, so the command is the console steps
            # plus the push. Which stack takes it comes from the row rather
            # than from an argument -- the credential authenticates against
            # one device, and one stack talks to that device.
            case ('derived', member, 'record') if member in devices.DEVICES:
                device = devices.DEVICES[member]
                _ = devices.deliver(
                    device,
                    stack=_stack(args, store, device.stack),
                    given={field.name: getattr(args, field.dest) for field in device.fields},
                )
            # §3's escrowed rows. generate -> escrow -> push (credentials.md
            # §4): the value reaches the slot §3 names for it in the same run,
            # and the push lives here rather than in the registry so that
            # opening an escrow never writes to a checkout.
            case ('derived', _, 'generate'):
                _ = _pushed(escrow.generate(registry, args.label), label=args.label)
            case ('derived', _, 'import'):
                _ = escrow.adopt(registry, args.label, _imported(args))
            case ('derived', _, 'recover'):
                return _recovered(args, escrow.Vault.open(store, registry))
            # The slot map's sink: one row at a time or every row whose value
            # lives elsewhere, each resolved, pushed and verified in the same
            # run.
            case ('derived', _, 'sync'):
                filled = slots.sync(_slots(args, store, registry), only=args.only)
                log.info('pushed %s', ', '.join(filled) if filled else 'nothing')
            case ('seed', member, action) if member in entries.SEEDS:
                raise KdbxError(f'`seed {member} {action}` is in the register (§2) but not yet implemented')
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled command {args.subject}')
    except (KdbxError, CredentialRejected, SlotRefused, EscrowError, AgeError) as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
