"""`operator-stack <stack> plan|up|pulumi …`: the command line of the driver.

    operator-stack github plan
    operator-stack github up
    operator-stack state-backend up --force
    operator-stack github pulumi config get githubAdminToken

The stack is the first argument and one of the operator-stack census
(`conventions.identity.OPERATOR_STACKS`); everything after `pulumi` goes to
`pulumi` whole, with the stack added and any word that would name another one
refused. Exit codes are `driver`'s: `plan` answers 0 when nothing is planned
and 1 when something is, and 2 is a refusal or a failed check. The
`state-backend` stack's `plan` and `up` answer 3 while the estate's backend
serves no stack and 4 while it does not answer, and its `up` holds a
replacement of the box for `--force` (`appliance`).
"""

from __future__ import annotations

import argparse
import logging

from kluster.conventions import identity
from kluster.lib import stack_environment, workstation
from kluster.scripts.operator_stack import appliance, driver

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='operator-stack',
        description=(
            'Run a stack no CI job runs, under its own backend and the operator passphrase, found in '
            'the desktop secret store, its slot or KLUSTER_OPERATOR_PASSPHRASE, or asked for at the '
            'terminal (docs/framework/pulumi.md §3.3).'
        ),
        epilog=(
            f'exit status: {driver.NOTHING_PLANNED} nothing planned, or applied; {driver.PLANNED} something '
            f'planned and not applied; {driver.FAILED} a refusal or a failed check. The {appliance.STACK} stack '
            f"answers {driver.RESTORE_OWED} while the estate's backend serves no stack -- a restore owed, or a "
            f'site before its first stack init -- and {driver.BACKEND_SILENT} while it does not answer.'
        ),
    )
    _ = parser.add_argument('stack', choices=sorted(identity.OPERATOR_STACKS), help='the operator stack to run')
    commands = parser.add_subparsers(dest='command', required=True, metavar='command')
    _ = commands.add_parser('plan', help='preview with a refresh: exit 0 when nothing is planned, 1 when something is')
    up = commands.add_parser('up', help='preview with a refresh, then apply what it plans once confirmed')
    _ = up.add_argument('--yes', '-y', action='store_true', help='apply without asking')
    _ = up.add_argument(
        '--force',
        action='store_true',
        help=f'{appliance.STACK} alone: apply a create, replacement or delete of the box, dumping it before it goes',
    )
    _ = up.add_argument(
        '--replace',
        action='store_true',
        help=f'{appliance.STACK} alone: replace the box whether or not anything moved, as --force does',
    )
    passthrough = commands.add_parser(
        'pulumi', help='any other pulumi command against the stack, as in `pulumi config get <key>`'
    )
    _ = passthrough.add_argument('args', nargs=argparse.REMAINDER, help='the pulumi command and its arguments')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    stack: str = args.stack
    try:
        run = driver.Run.open(stack, workstation.repo_root())
        match args.command:
            case 'plan':
                return run.plan()
            case 'up':
                return run.up(yes=bool(args.yes), force=bool(args.force), replace=bool(args.replace))
            case _:
                return run.passthrough(list[str](args.args))
    except (
        driver.Refused,
        appliance.Refused,
        stack_environment.EnvironmentRefused,
        workstation.WorkstationError,
    ) as exc:
        log.error('%s', exc)
        return driver.FAILED
