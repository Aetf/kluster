"""`credentials` — the executable form of the credential register.

The command tree is the register's two tables (docs/credentials.md), not the
accounts behind them: `seed` holds one member per §2 row, `derived` one family
per §3 row. Reading `credentials --help` beside the register should show the
same shape twice; a command with no row, or a row with no command, is the bug
that discipline is meant to surface.

Every action is mint -> push to every slot -> verify, and therefore idempotent:
rotation is a re-run, not a second procedure.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import shlex
import sys
from pathlib import Path

from . import b2, derived, entries, lifecycle, masters, oci_iam, pulumi_config, seeds
from .kdbx import PATH_ENV, KdbxError, KdbxStore
from .masters import CredentialRejected
from .pulumi_config import SlotRefused

log = logging.getLogger(__name__)

#: Where `state-backend` leaves the client bundle, and therefore where the URL
#: of the state backend is read from. Every command that has to reach the
#: backend takes it as an option defaulting to this.
BUNDLE_DIR = Path.home() / '.config' / 'kluster' / 'state-backend'

#: Which command runs when. The tree says what exists; this says what to do
#: with it, because "one subcommand per register row" answers neither "where
#: do I start" nor "is this the one that destroys something".
_ORDER = """when to run what (docs/credentials.md §4):

  bring-up, from nothing
    0. credentials master <root> remember
         Puts an account root (§2) in the desktop secret store, once per
         machine, so the mints below ask for nothing. Skip it and they
         prompt instead, which is how a headless run works.
    1. credentials bootstrap
         Fills a kit with every seed in §2, creating the kit if it is absent.
         Stops at each credential no API can create and prints the console
         steps. Re-run it to resume: it skips what is already there.
    2. state-backend provision
         The Pulumi state backend, which every stack needs before it can act.
    3. eval "$(credentials derive env)"
         PULUMI_CONFIG_PASSPHRASE (derived, stored nowhere) and
         PULUMI_BACKEND_URL (from the bundle step 2 wrote).
    4. credentials derived cloudflare zones
         Mints the zone-scoped Cloudflare token from the seed and writes
         it into the dns stack's config, which is then committed. One
         §3 row per command; re-running one rotates it.

  on a workstation that develops without the kit
    credentials derive passphrase > .pulumi.secret
         Caches the passphrase where mise.toml reads it, so a local
         `pulumi preview` needs neither the kit nor an eval. The client
         bundle has to be copied to that machine too.

  day to day
    Nothing. No runtime credential is in the kit, so no operation outside
    bring-up, rotation and the yearly offline day opens it (§2.1).

  when one seed is lost
    credentials bootstrap --only <member>
         Re-creates that row alone; the rest of the kit is untouched.

  one-time repair
    credentials seed oci domain
         Records the tenancy's identity domain on an OCI row written before
         that attribute existed. Without it a rotation mints a successor and
         cannot retire what it supersedes, because the legacy delete call is
         refused. Borrows the account root, once.

  rotation (§4.2)
    credentials rotate --into <new kit>
         Writes a *new* database. Keep the retired one until the last secret
         derived from it has expired -- backups encrypted under the old
         derivation seed cannot be re-encrypted retroactively (§2.2).

  looking without changing
    credentials kdbx ls | show <entry>
    credentials master ls        which account roots the secret store holds
    credentials kdbx remember    stores the kit's master password in the
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
        help=f'the seed kit (default: ${PATH_ENV})',
    )
    families = parser.add_subparsers(dest='family', required=True, metavar='<family>')

    # The three things done to a kit (§4). Each walks §2's table in order and
    # skips what is already there, so an interrupted run is resumed by
    # re-running it rather than by remembering where it stopped.
    boot = families.add_parser('bootstrap', help='fill a kit with every seed (§4.1); creates the kit if absent')
    _ = boot.add_argument('--only', default=None, metavar='<member>', help='create just this seed (repair, seed loss)')

    der = families.add_parser('derive', help='the secrets computed from the derivation seed (§2.2)')
    der_actions = der.add_subparsers(dest='action', required=True, metavar='<action>')
    env = der_actions.add_parser('env', help='shell exports for a Pulumi run; use with eval')
    _ = env.add_argument(
        '--bundle-dir',
        type=Path,
        default=BUNDLE_DIR,
        help='where `state-backend` wrote the client bundle',
    )
    # `env` is for a shell; this is for a file. A workstation that develops
    # against the backend needs the passphrase on every `pulumi preview`, and
    # the kit is not on every workstation (§2.1).
    _ = der_actions.add_parser('passphrase', help='the passphrase alone, for a workstation cache file')

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
            if seed.member == 'derivation'
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
        default=BUNDLE_DIR,
        help='where `state-backend` wrote the client bundle',
    )

    # The account roots (§2) are not in the kit and not in any database this
    # repository opens: each lives in the desktop secret store under its own
    # key, and a machine without one prompts. This family is how they get
    # there, and the only thing that ever writes them.
    master_cmd = families.add_parser('master', help='the account roots the mints borrow (§2)')
    roots = master_cmd.add_subparsers(dest='member', required=True, metavar='<root>')
    listing = roots.add_parser('ls', help='which roots the secret store holds; prints no values')
    listing.set_defaults(action='ls')
    for account in masters.ROOTS.values():
        root_cmd = roots.add_parser(account.member, help=account.title)
        root_actions = root_cmd.add_subparsers(dest='action', required=True, metavar='<action>')
        _ = root_actions.add_parser('remember', help='prompt for it and store it in the desktop secret store')
        _ = root_actions.add_parser('forget', help='remove it from the secret store')

    return parser


def _master(args: argparse.Namespace) -> int:
    """The account-root commands, which need no kit and open no database."""
    if args.action == 'ls':
        for account in masters.ROOTS.values():
            held = masters.stored(account)
            state = (
                'in the secret store'
                if all(held.values())
                else 'missing: ' + ', '.join(name for name, present in held.items() if not present)
            )
            print(f'{account.member}: {state}')
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
    path = args.kdbx or (Path(raw).expanduser() if (raw := os.environ.get(PATH_ENV)) else None)
    if args.family == 'bootstrap' and path is not None and not path.exists():
        log.info('no kit at %s; creating it', path)
        return KdbxStore.create(path, getpass.getpass(f'new master password for {path.name}: '))
    return KdbxStore.from_env(args.kdbx)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)

    try:
        if args.family == 'master':
            return _master(args)
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
            case ('derive', _, 'env'):
                # Written to stdout for `eval`, and refused when stdout is the
                # terminal: a passphrase in the scrollback is a passphrase in
                # the next screen-share.
                if sys.stdout.isatty():
                    log.error('this prints a passphrase; pipe it: eval "$(credentials derive env)"')
                    return 1
                for name, value in lifecycle.environment(store, args.bundle_dir).items():
                    print(f'export {name}={shlex.quote(value)}')
            case ('derive', _, 'passphrase'):
                if sys.stdout.isatty():
                    log.error('this prints a passphrase; redirect it: credentials derive passphrase > .pulumi.secret')
                    return 1
                print(seeds.pulumi_passphrase(seeds.load_seed(store)))
            case ('bootstrap', _, _):
                created = lifecycle.bootstrap(store, prompt=input, only=args.only)
                log.info('created %s', ', '.join(created) if created else 'nothing; the kit was already complete')
            case ('rotate', _, _):
                successor = KdbxStore.create(args.into, getpass.getpass(f'master password for {args.into.name}: '))
                rotated = lifecycle.rotate(store, successor, prompt=input, only=args.only)
                log.info('rotated %s into %s', ', '.join(rotated), args.into)
                log.warning('keep %s until the last secret derived from it has expired (§2.2)', store.path)
            # One row at a time, through the same dispatch `bootstrap` walks:
            # a single-row repair and a whole-kit fill must not be able to
            # write a row two different ways.
            case ('seed', member, 'create') if member in entries.SEEDS:
                lifecycle.create_seed(entries.SEEDS[member], kit=store, prompt=input, entry=args.entry)
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
            # variables a `pulumi` run needs, derived from the kit that is
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
    except (KdbxError, CredentialRejected, SlotRefused) as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
