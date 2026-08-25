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
import logging
import sys
from pathlib import Path

from . import b2, seeds
from .kdbx import MASTER_PATH_ENV, PATH_ENV, KdbxError, KdbxStore

log = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='credentials', description=__doc__)
    _ = parser.add_argument(
        '--kdbx',
        type=Path,
        default=None,
        help=f'the seed kit (default: ${PATH_ENV})',
    )
    families = parser.add_subparsers(dest='family', required=True, metavar='<family>')

    kdbx_cmd = families.add_parser('kdbx', help='the offline store itself (§2.1)')
    kdbx_actions = kdbx_cmd.add_subparsers(dest='action', required=True, metavar='<action>')
    ls = kdbx_actions.add_parser('ls', help='list entry paths')
    _ = ls.add_argument('group', nargs='?', default='/')
    show = kdbx_actions.add_parser('show', help="an entry's non-secret attributes")
    _ = show.add_argument('entry')

    # One member per §2 row. They are peers: the derivation seed is the source
    # of everything *derived*, the others the source of everything *minted* --
    # neither is above the other, which is why no member is called a root.
    seed_cmd = families.add_parser('seed', help='the seed layer (§2), one member per row')
    seeds_ = seed_cmd.add_subparsers(dest='member', required=True, metavar='<member>')

    derivation = seeds_.add_parser('derivation', help='the 32 bytes behind every derived secret (§2.2)')
    _ = derivation.add_argument('--entry', default=seeds.SEED_ENTRY, help='entry holding the seed')
    derivation_actions = derivation.add_subparsers(dest='action', required=True, metavar='<action>')
    _ = derivation_actions.add_parser('init', help='create it (bring-up, once)')

    b2_cmd = seeds_.add_parser('b2', help='the B2 seed key')
    _ = b2_cmd.add_argument('--entry', default='seeds/B2 seed key', help='entry holding the seed key')
    b2_actions = b2_cmd.add_subparsers(dest='action', required=True, metavar='<action>')
    create = b2_actions.add_parser('create', help='mint it from the account master key (bring-up, or seed loss)')
    _ = create.add_argument(
        '--master-entry',
        required=True,
        help='entry holding the account master key (id as username, key as password)',
    )
    # The account roots live in the personal estate, not in the kit (§2), so
    # this is the one action that opens two databases.
    _ = create.add_argument(
        '--master-kdbx',
        type=Path,
        default=None,
        help=f'the database holding the account master key (default: ${MASTER_PATH_ENV})',
    )
    _ = b2_actions.add_parser('rotate', help='have the seed mint and install its successor')

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = _parser().parse_args(argv)

    try:
        store = KdbxStore.from_env(args.kdbx)

        match (args.family, getattr(args, 'member', None), args.action):
            case ('kdbx', _, 'ls'):
                for entry in store.entries(args.group):
                    print(entry)
            case ('kdbx', _, 'show'):
                for name, value in store.describe(args.entry).items():
                    print(f'{name}: {value}')
            case ('seed', 'derivation', 'init'):
                seeds.init_seed(store, args.entry)
            case ('seed', 'b2', 'create'):
                master = KdbxStore.from_env(args.master_kdbx, env=MASTER_PATH_ENV, flag='--master-kdbx')
                _ = b2.create_seed(
                    master=master,
                    seeds=store,
                    master_entry=args.master_entry,
                    seed_entry=args.entry,
                )
            case ('seed', 'b2', 'rotate'):
                _ = b2.rotate_seed(store, seed_entry=args.entry)
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled command {args.family} {args.action}')
    except (KdbxError, b2.CredentialRejected) as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
