"""`credentials` — the executable form of the credential register.

One subcommand per credential family (docs/credentials.md §4): mint, push to
every slot, verify. The same command is what a rotation playbook runs, so
rotation is a re-run rather than a second procedure.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import b2
from .kdbx import PATH_ENV, KdbxError, KdbxStore

log = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='credentials', description=__doc__)
    _ = parser.add_argument(
        '--kdbx',
        type=Path,
        default=None,
        help=f'the cluster KeePassXC database (default: ${PATH_ENV})',
    )
    families = parser.add_subparsers(dest='family', required=True)

    kdbx_cmd = families.add_parser('kdbx', help='inspect the offline store')
    kdbx_actions = kdbx_cmd.add_subparsers(dest='action', required=True)
    ls = kdbx_actions.add_parser('ls', help='list entry paths')
    _ = ls.add_argument('group', nargs='?', default='/')

    b2_cmd = families.add_parser('b2', help='the B2 management key')
    _ = b2_cmd.add_argument(
        '--master-entry',
        required=True,
        help='entry holding the master application key (id as username, key as password)',
    )
    _ = b2_cmd.add_argument('--entry', default='tokens/B2 management key', help='entry the minted key is written to')
    _ = b2_cmd.add_argument('--name', default=b2.DEFAULT_KEY_NAME, help='B2 key name')
    b2_actions = b2_cmd.add_subparsers(dest='action', required=True)
    _ = b2_actions.add_parser('mint', help='create a management key and store it')
    prune = b2_actions.add_parser('prune', help='delete superseded keys of the same name')
    _ = prune.add_argument('keep', help='key id currently in the slots')

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = _parser().parse_args(argv)

    try:
        store = KdbxStore.from_env(args.kdbx)

        match (args.family, args.action):
            case ('kdbx', 'ls'):
                for entry in store.entries(args.group):
                    print(entry)
            case ('b2', 'mint'):
                _ = b2.mint(store, master_entry=args.master_entry, entry=args.entry, name=args.name)
            case ('b2', 'prune'):
                b2.prune(store, master_entry=args.master_entry, keep=args.keep, name=args.name)
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled command {args.family} {args.action}')
    except KdbxError as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
