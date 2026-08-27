"""`credentials` — the executable form of the credential register.

The command tree is the register's tables (docs/credentials.md), not the
accounts behind them: `seed` holds one member per §2 row, `escrow` one action
per thing that can be done to §2.2's generated secrets, and `derived` one
family per §3 row. Reading `credentials --help` beside the register should
show the same shapes; a command with no row, or a row with no command, is the
bug that discipline is meant to surface.

Every action is mint -> push to every slot -> verify, and therefore idempotent:
rotation is a re-run, not a second procedure.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import shlex
import sys
from pathlib import Path

from . import b2, derived, entries, escrow, lifecycle, masters, oci_iam, pulumi_config, workstation
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
    0. credentials master <root> remember
         Keeps an account root (§2) on this machine, once, so the mints
         below ask for nothing. Every root is looked up the same way:
         desktop secret store, then its token file, then its environment
         variable, then a prompt -- so skipping this costs a prompt rather
         than a failure, which is how a headless run works.
    1. credentials bootstrap
         Fills a kit with every seed in §2, creating the kit if it is absent.
         The recovery key is one of them, and creating it also writes
         escrow/RECIPIENTS. Stops at each credential no API can create and
         prints the console steps. Re-run it to resume.
    2. state-backend provision
         The Pulumi state backend, which every stack needs before it can act,
         and the first thing to escrow: it generates the CA and the backup
         identities it is about to install and commits their ciphertexts.
    3. credentials escrow generate pulumi/passphrase
         The one escrowed label no other command mints. It writes the
         workstation slot as well as the ciphertext, so mise.toml finds it.
    4. eval "$(credentials escrow env)"
         PULUMI_CONFIG_PASSPHRASE (recovered from escrow) and
         PULUMI_BACKEND_URL (from the bundle step 2 wrote).
    5. credentials derived cloudflare zones
         Mints the zone-scoped Cloudflare token from the seed and writes
         it into the dns stack's config, which is then committed. One
         §3 row per command; re-running one rotates it.

  on a workstation that develops without the kit
    Copy the .credentials directory from a machine that has one: the
    passphrase slot and the client bundle come with it. On a machine that
    does hold the kit, `credentials escrow recover pulumi/passphrase`
    writes that slot, where mise.toml reads it on every pulumi run.

  day to day
    Nothing. No runtime credential is in the kit, so no operation outside
    bring-up, rotation and the yearly offline day opens it (§2.1).
    `credentials escrow check` needs no kit at all.

  when one seed is lost
    credentials bootstrap --only <member>
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
    credentials escrow generate <label>
         A new generation for that label alone, adopted by re-running what
         consumes it. Nothing else moves.

  rotating the kit (§4.2)
    credentials rotate --into <new kit>
         Writes a *new* database, and re-wraps every escrow ciphertext to
         the successor recovery key. No production secret changes value, so
         the retired file is destroyable once the run is verified.

  looking without changing
    credentials escrow check      every expected label present, every
                                  ciphertext an age file, generations dense
    credentials kdbx ls | show <entry>
    credentials master ls         which account roots this machine holds, and
                                  which layer of the chain each comes from
    credentials kdbx remember     stores the kit's master password in the
                                  desktop secret store, so a long run is not
                                  guarded by a password typed into it
"""


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
    families = parser.add_subparsers(dest='family', required=True, metavar='<family>')

    # The three things done to a kit (§4). Each walks §2's table in order and
    # skips what is already there, so an interrupted run is resumed by
    # re-running it rather than by remembering where it stopped.
    boot = families.add_parser('bootstrap', help='fill a kit with every seed (§4.1); creates the kit if absent')
    _ = boot.add_argument('--only', default=None, metavar='<member>', help='create just this seed (repair, seed loss)')

    # The escrow (§2.2): random secrets whose ciphertexts are committed, and
    # the one recovery key that opens them. `generate` and the ciphertext are
    # a single act, so no command here hands out a secret the registry does
    # not carry.
    esc = families.add_parser('escrow', help='the generated secrets and their committed ciphertexts (§2.2)')
    esc_actions = esc.add_subparsers(dest='action', required=True, metavar='<action>')
    _ = esc_actions.add_parser('init', help='create the recovery key: kit row plus escrow/RECIPIENTS')
    generate = esc_actions.add_parser('generate', help="mint a label's next generation and escrow it")
    _ = generate.add_argument('label', help=f'one of: {", ".join(sorted(escrow.register()))}')
    adopt = esc_actions.add_parser('import', help="escrow a value that already exists as the label's next generation")
    _ = adopt.add_argument('label')
    _ = adopt.add_argument(
        '--from-slot',
        action='store_true',
        help='read it from its workstation slot instead of standard input',
    )
    recover = esc_actions.add_parser('recover', help='open one escrowed secret with the kit')
    _ = recover.add_argument('label')
    _ = recover.add_argument('--generation', type=int, default=None, help='default: the newest')
    _ = recover.add_argument('--stdout', action='store_true', help='print it even though it has a slot')
    _ = esc_actions.add_parser('rewrap', help='re-encrypt every ciphertext to the recipients now on file')
    _ = esc_actions.add_parser('check', help='what is missing or malformed; needs no kit')
    env = esc_actions.add_parser('env', help='shell exports for a Pulumi run; use with eval')
    _ = env.add_argument(
        '--bundle-dir',
        type=Path,
        default=workstation.bundle_dir(),
        help='where `state-backend` wrote the client bundle',
    )

    rot = families.add_parser('rotate', help='write a new kit in which every seed is replaced (§4.2)')
    _ = rot.add_argument('--into', type=Path, required=True, help='path for the successor kit; must not exist')
    _ = rot.add_argument('--only', default=None, metavar='<member>', help='rotate just this seed')

    kdbx_cmd = families.add_parser('kdbx', help='the offline store itself (§2.1)')
    kdbx_actions = kdbx_cmd.add_subparsers(dest='action', required=True, metavar='<action>')
    ls = kdbx_actions.add_parser('ls', help='list entry paths')
    _ = ls.add_argument('group', nargs='?', default='/')
    show = kdbx_actions.add_parser('show', help="an entry's non-secret attributes")
    _ = show.add_argument('entry')
    # The kit is the one database a run opens, and a bring-up holds it open
    # for minutes; guarding that with a password typed into an unwatched
    # process is what this avoids.
    _ = kdbx_actions.add_parser('remember', help="store this database's master password in the desktop secret store")
    _ = kdbx_actions.add_parser('forget', help='remove it from the secret store')

    # The tree is generated from §2's table rather than written out, so a
    # seed that exists in the register and nowhere in the code shows up as a
    # subcommand that refuses to run -- not as a subcommand that is missing.
    seed_cmd = families.add_parser('seed', help='the seed layer (§2), one member per row')
    members = seed_cmd.add_subparsers(dest='member', required=True, metavar='<member>')
    for seed in entries.SEEDS.values():
        member = members.add_parser(seed.member, help=f'mints {seed.mints}')
        _ = member.add_argument('--entry', default=seed.entry, help=f'entry holding it (default: {seed.entry})')
        actions = member.add_subparsers(dest='action', required=True, metavar='<action>')
        _ = actions.add_parser(
            'create',
            help='generate it (bring-up, once)'
            if seed.member == 'recovery'
            else 'mint it from the account root (bring-up, or seed loss)',
        )
        if seed.self_reproducing:
            _ = actions.add_parser('rotate', help='have the seed mint and install its successor')
        if seed.repair is not None:
            _ = actions.add_parser(seed.repair[0], help=seed.repair[1])

    # The other half of the register: §3's rows, each minted from a seed and
    # pushed into the slot its consumer reads. A row joins this family when
    # that consumer exists -- a mint with nowhere to deliver would park a
    # secret, which is the one thing the register forbids outright.
    derived_cmd = families.add_parser('derived', help='the credentials minted from a seed into a slot (§3)')
    rows = derived_cmd.add_subparsers(dest='member', required=True, metavar='<row>')
    cloudflare_row = rows.add_parser('cloudflare', help='the tokens the Cloudflare seed mints (§3)')
    cloudflare_actions = cloudflare_row.add_subparsers(dest='action', required=True, metavar='<token>')
    zones = cloudflare_actions.add_parser(
        'zones',
        help="the zone-scoped provider token, into the dns stack's config secret",
    )
    _ = zones.add_argument('--entry', default=derived.SEED_ENTRY, help=f'the seed row (default: {derived.SEED_ENTRY})')
    _ = zones.add_argument(
        '--stack',
        default=derived.ZONES_STACK,
        help=f'the stack whose config takes the token (default: {derived.ZONES_STACK})',
    )
    _ = zones.add_argument(
        '--bundle-dir',
        type=Path,
        default=workstation.bundle_dir(),
        help='where `state-backend` wrote the client bundle',
    )

    # The account roots (§2) are not in the kit and not in any database this
    # repository opens: each is looked up through one chain -- desktop secret
    # store, token file, environment variable, prompt (`masters.py`). This
    # family is how they get onto a machine, and the only thing that writes
    # them.
    master_cmd = families.add_parser('master', help='the account roots the workstation borrows (§2)')
    roots = master_cmd.add_subparsers(dest='member', required=True, metavar='<root>')
    listing = roots.add_parser('ls', help='which roots this machine holds, and where; prints no values')
    listing.set_defaults(action='ls')
    for account in masters.ROOTS.values():
        root_cmd = roots.add_parser(account.member, help=account.title)
        root_actions = root_cmd.add_subparsers(dest='action', required=True, metavar='<action>')
        _ = root_actions.add_parser('remember', help='prompt for it and keep it on this machine')
        _ = root_actions.add_parser('forget', help='remove it from the secret store and from its token file')

    return parser


def _layers(held: dict[str, str | None]) -> str:
    """One root's line in `master ls`: where its fields are, never what they are.

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


def _master(args: argparse.Namespace) -> int:
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
    """The seed kit, created on the spot if `bootstrap` is starting from none.

    Every other command needs it to exist already: creating one by accident,
    because a path was mistyped, would look like an empty kit rather than a
    missing one.
    """
    path = args.kdbx or default_path()
    if args.family == 'bootstrap' and not path.exists():
        log.info('no kit at %s; creating it', path)
        return KdbxStore.create(path, getpass.getpass(f'new master password for {path.name}: '))
    return KdbxStore.from_env(args.kdbx)


def _check(registry: escrow.Registry) -> int:
    """`escrow check`, which is the one command that opens nothing.

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
    """The value `escrow import` is to escrow: a slot, or standard input.

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
        if args.family == 'master':
            return _master(args)
        registry = escrow.Registry.open(args.escrow)
        # Ordered before the kit deliberately: the point of `check` is that a
        # clone with no offline database can still say whether the registry is
        # whole.
        if (args.family, getattr(args, 'action', None)) == ('escrow', 'check'):
            return _check(registry)
        store = _kit(args)

        # `bootstrap` and `rotate` have no <action> level, so the attribute
        # does not exist on their namespaces; the guard mirrors `member`'s.
        match (args.family, getattr(args, 'member', None), getattr(args, 'action', None)):
            case ('kdbx', _, 'ls'):
                for entry in store.entries(args.group):
                    print(entry)
            case ('kdbx', _, 'show'):
                for name, value in store.describe(args.entry).items():
                    print(f'{name}: {value}')
            case ('kdbx', _, 'remember'):
                # Prove it opens the database before storing it: a remembered
                # password that does not work is worse than none.
                password = getpass.getpass(f'master password for {store.path.name}: ')
                store.unlock_with(password)
                store.remember(password)
            case ('kdbx', _, 'forget'):
                store.forget()
            case ('escrow', _, 'init'):
                identity = escrow.init(store, registry)
                log.info('the recovery key is in the kit; commit %s', registry.recipients_file)
                log.info('it encrypts to %s', identity.public)
                for label in escrow.missing(registry):
                    log.warning('nothing escrowed for %s yet: credentials escrow generate %s', label, label)
            case ('escrow', _, 'generate'):
                # generate -> escrow -> push (credentials.md §4): the value
                # reaches the slot §3 names for it in the same run, and the
                # push lives here rather than in the registry so that opening
                # an escrow never writes to a checkout.
                _ = _pushed(escrow.generate(registry, args.label), label=args.label)
            case ('escrow', _, 'import'):
                _ = escrow.adopt(registry, args.label, _imported(args))
            case ('escrow', _, 'recover'):
                return _recovered(args, escrow.Vault.open(store, registry))
            case ('escrow', _, 'rewrap'):
                _ = escrow.rewrap(registry, identities=[store.get(escrow.RECOVERY_ENTRY)])
            case ('escrow', _, 'env'):
                # Written to stdout for `eval`, and refused when stdout is the
                # terminal: a passphrase in the scrollback is a passphrase in
                # the next screen-share.
                if sys.stdout.isatty():
                    log.error('this prints a passphrase; pipe it: eval "$(credentials escrow env)"')
                    return 1
                for name, value in lifecycle.environment(store, args.bundle_dir, registry).items():
                    print(f'export {name}={shlex.quote(value)}')
            case ('bootstrap', _, _):
                created = lifecycle.bootstrap(store, prompt=input, only=args.only, registry=registry)
                log.info('created %s', ', '.join(created) if created else 'nothing; the kit was already complete')
            case ('rotate', _, _):
                # Before the successor exists, not once `rotate` reaches its
                # walk: a `--only` that names no row would otherwise leave a
                # new database file behind with nothing rotated into it.
                lifecycle.require_member(args.only)
                successor = KdbxStore.create(args.into, getpass.getpass(f'master password for {args.into.name}: '))
                rotated = lifecycle.rotate(store, successor, prompt=input, only=args.only, registry=registry)
                log.info('rotated %s into %s', ', '.join(rotated), args.into)
                if 'recovery' in rotated:
                    log.warning('commit %s: every ciphertext now opens with the successor key alone', registry.root)
            # One row at a time, through the same dispatch `bootstrap` walks:
            # a single-row repair and a whole-kit fill must not be able to
            # write a row two different ways.
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
            # §3's rows. The stack's configuration is opened with the same two
            # variables a `pulumi` run needs, recovered with the kit that is
            # already open rather than expected in the environment: one command
            # is one credential delivered, not a shell that has to be prepared
            # first.
            case ('derived', 'cloudflare', 'zones'):
                stack = pulumi_config.Stack(
                    name=args.stack,
                    directory=pulumi_config.project_dir(),
                    env=lifecycle.environment(store, args.bundle_dir),
                )
                _ = derived.cloudflare_zones(store, stack=stack, seed_entry=args.entry)
            case ('seed', member, action) if member in entries.SEEDS:
                raise KdbxError(f'`seed {member} {action}` is in the register (§2) but not yet implemented')
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled command {args.family}')
    except (KdbxError, CredentialRejected, SlotRefused, EscrowError, AgeError) as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
