"""`state-backend` -- inspect, dump and restore the appliance.

The appliance itself is the `state-backend` stack's, created, converged and
replaced by `operator-stack state-backend up` (physical/state-backend.md §1);
this command keeps what declares nothing (rfc-006 §4.5). `dump` and `restore`
are the standalone moves of the playbooks (physical/state-backend.md §7), each
of which verifies rather than reports, and the restore a run of the stack
owes after a replacement it took no dump for. `census` reads what the state
table holds, prunes the rows nothing reads and holds every value to the
ceiling the pooler's cap is derived for, `render` renders a scratch
box for the rebuild drill, `ssh` logs in for diagnosis, `bundle` writes a
client bundle, and `pins` checks the pinned artifacts. `release-lock` is the
command written to run somewhere other than a workstation: the step of a
deploy job that deletes the lock its own `pulumi up` left (`locks.py`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import socket
import sys
import tempfile
from pathlib import Path

from kluster.conventions import CompartmentMissing
from kluster.lib import config as lib_config
from kluster.lib import stack_environment
from kluster.lib.pulumi_cli import PulumiRefused
from kluster.lib.state_backend import committed, readiness, render, settings, state
from kluster.lib.state_backend.state import StateError
from kluster.scripts.credentials import escrow, pki, workstation
from kluster.scripts.credentials.age import AgeError
from kluster.scripts.credentials.escrow import EscrowError
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.credentials.workstation import WorkstationError

from . import census, config, locks, provision

log = logging.getLogger(__name__)

#: What `main` turns into one line and an exit status of 1: every refusal a
#: module this program imports can raise. A refusal is a decision with the
#: repair in its message, and an operator reads a traceback as a crash instead,
#: with that repair buried under the stack. The census is the import closure
#: rather than today's call paths, because that is the boundary a test can
#: hold (`test_provision.py` walks it in both directions); the members no
#: current call reaches -- `PulumiRefused`, which `state.stacks` and the
#: configuration's reads translate into their own; `CompartmentMissing`,
#: which `conventions` raises for a compartment it does not record; and
#: `lib_config.ShapeRefused`, which the readers of a stack's structured
#: configuration raise -- are here so that a call that reaches one tomorrow
#: gets the line rather than the traceback.
REFUSALS = (
    AgeError,
    CompartmentMissing,
    EscrowError,
    KdbxError,
    PulumiRefused,
    SlotRefused,
    StateError,
    WorkstationError,
    committed.Refused,
    lib_config.ShapeRefused,
    provision.DigestMissing,
    provision.DocumentIncomplete,
    stack_environment.EnvironmentRefused,
)


def _hours(span: dt.timedelta) -> str:
    """A span as the hours a reader compares a row's age in."""
    return f'{span.total_seconds() / 3600:.1f} h'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='state-backend', description=__doc__)
    _ = parser.add_argument('--kdbx', type=Path, default=None, help='the cluster KeePassXC database')
    _ = parser.add_argument(
        '--escrow',
        type=Path,
        default=None,
        help=f'the escrow registry (default: the {escrow.DIRECTORY}/ directory of this checkout)',
    )
    actions = parser.add_subparsers(dest='action', required=True)

    render = actions.add_parser('render', help="render a scratch box's Ignition config without touching the cloud")
    _ = render.add_argument('--address', default='192.0.2.10', help='address to issue the server certificate for')

    # Diagnosis only: the box is never configured by hand (state-backend.md
    # §1). Needs no offline database and no OCI credential.
    ssh_cmd = actions.add_parser('ssh', help='log in to the appliance for diagnosis, held to the committed host key')
    _ = ssh_cmd.add_argument('command', nargs='*', help='run this instead of a login shell')
    _ = actions.add_parser('pins', help='check the pinned artifacts against their digests')

    bundle = actions.add_parser('bundle', help='write a client bundle (ca/cert/key/url)')
    _ = bundle.add_argument('name', choices=['ci', 'operator'])
    _ = bundle.add_argument('--address', required=True)
    _ = bundle.add_argument('--directory', type=Path, default=workstation.bundle_dir())

    dump = actions.add_parser('dump', help='take a dump of the live state, encrypted like the nightly one')
    _ = dump.add_argument(
        '--output',
        type=Path,
        default=None,
        help=f'where to write it (default: ./{settings.NAME}-<UTC stamp>.dump.age)',
    )
    _ = dump.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle whose connection string to dump over',
    )

    # Needs no offline database: the client bundle is the whole credential.
    counting = actions.add_parser(
        'census',
        help='count the state table by kind of key, prune the rows nothing reads, and check the ceiling',
        description=census.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    modes = counting.add_mutually_exclusive_group()
    _ = modes.add_argument(
        '--prune-superseded',
        action='store_true',
        help='also list each .bak, history/ and backups/ copy stored in a format its stack no longer writes',
    )
    _ = modes.add_argument(
        '--prune-expired',
        action='store_true',
        help=(
            f'also list each row the retention rule expires: past {_hours(settings.EXPIRY_AGE)}, every checkpoint copy, '
            f'every update record beyond the newest {settings.HISTORY_RECORDS} of its stack, and every .bak '
            'whose stack has no checkpoint'
        ),
    )
    _ = counting.add_argument(
        '--yes',
        action='store_true',
        help='with --prune-superseded or --prune-expired, delete the rows it lists',
    )
    _ = counting.add_argument(
        '--ceiling',
        action='store_true',
        help=(
            "after any prune, read every row's value size and fail, naming each row, where one exceeds "
            f'{settings.LARGEST_ROW_BYTES:,} bytes (settings.LARGEST_ROW_BYTES)'
        ),
    )
    _ = counting.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle whose connection string to read the state table over',
    )

    # What a deploy's `Up` step runs after its `pulumi up` (framework/ci.md
    # §3.3). Needs no offline database: the client bundle is the whole
    # credential.
    releasing = actions.add_parser(
        'release-lock',
        help='delete the lock a stack holds for the pulumi process this machine started, and no other',
        description=locks.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'exit status: 0 the backend answered, and the stack holds no lock that process took; '
            f'1 the backend did not answer within {readiness.duration(locks.WAIT)}, or the bundle is missing.'
        ),
    )
    _ = releasing.add_argument('--stack', required=True, help='the stack, as `pulumi --stack` names it')
    _ = releasing.add_argument(
        '--start-file',
        type=Path,
        required=True,
        help=(
            'the file recorded before the pulumi process started: its pid, then the time in seconds since the '
            'epoch, on one line; a missing one proves no lock'
        ),
    )
    _ = releasing.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle whose connection string to read the locks over',
    )

    restore = actions.add_parser('restore', help='feed a dump into a box that serves no stack')
    _ = restore.add_argument('dump', type=Path, help='an age-encrypted dump, or a raw pg_dump custom-format archive')
    # The unattended drill (§7.3) runs in the ops repo with one age key in a
    # repository secret and no kit at all, so naming a key here has to be
    # enough on its own -- including for opening the kit this run then never
    # touches.
    _ = restore.add_argument(
        '--identity-file',
        type=Path,
        default=None,
        help='decrypt with the age identity in this file instead of opening the escrow',
    )
    _ = restore.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle whose connection string to restore over',
    )
    _ = restore.add_argument(
        '--force',
        action='store_true',
        help='restore even though the target backend already serves stacks',
    )

    return parser


def _dump(store: KdbxStore, *, registry: escrow.Registry, bundle_dir: Path, output: Path | None) -> int:
    """A dump on demand, in the form the appliance's own timer writes.

    Encrypted to the escrow's recipients rather than left in plain text --
    the generations the committed recipients file names, which `credentials
    derived check` holds to the escrow, and the drill recipient on file --
    and read for a stack checkpoint before it is called a dump: an archive
    holding none is a dump of a backend serving no stack -- what a replaced
    box holds until its restore -- and the operator taking one is usually
    about to destroy the box it came from (§7.2).
    """
    destination = (output if output is not None else Path(state.dump_name())).resolve()
    state.refuse_to_overwrite(destination)

    log.info('[1/2] opening the escrow with the kit, for the recipients the appliance encrypts its dumps to')
    recipients = config.age_recipients(escrow.Vault.open(store, registry))
    log.info('[2/2] taking the dump')
    state.write_dump(destination, bundle_dir=bundle_dir, recipients=recipients)
    return 0


def _restore(
    store: KdbxStore | None,
    *,
    registry: escrow.Registry,
    bundle_dir: Path,
    source: Path,
    identity: Path | None,
    force: bool,
) -> int:
    """Feed a dump into a box, and prove afterwards that it took.

    The order is the one that makes each failure cheap: refuse to overwrite a
    populated backend before anything is decrypted, verify the archive before
    it touches the database, restore in one transaction, and only then
    report — by asking `pulumi` what the backend now serves.
    """
    target = state.connection(bundle_dir)
    log.info('[1/5] asking the target backend what it already holds')
    occupied = state.served(target)
    if occupied and not force:
        log.error('%s already serves %d stack(s): %s', state.endpoint(target.url), len(occupied), ', '.join(occupied))
        log.error('restoring over live state is `--force`; a replacement restores into a box that has none')
        return 1
    if occupied:
        log.warning(
            '--force: restoring over the %d stack(s) %s already serves', len(occupied), state.endpoint(target.url)
        )

    with tempfile.TemporaryDirectory(prefix=f'{settings.NAME}-') as tmp:
        archive = source
        if state.encrypted(source):
            archive = Path(tmp) / 'state.dump'
            if identity is not None:
                log.info('[2/5] decrypting with the identity in %s', identity)
                identities = state.identity_file(identity)
            else:
                if store is None:  # pragma: no cover - main opens a kit whenever there is no identity file
                    raise StateError('no kit and no --identity-file: nothing can open this dump')
                log.info('[2/5] opening the escrow with the kit, for the identities the dump may be under')
                identities = config.backup_identities(escrow.Vault.open(store, registry))
            state.decrypt(source, archive, identities)
        else:
            log.info('[2/5] %s is a plain archive; nothing to decrypt', source)
        log.info('[3/5] verifying the archive before it touches the database')
        _ = state.verify_dump(archive)
        log.info('[4/5] restoring over the client bundle in %s', bundle_dir)
        state.pg_restore(target, archive)

    log.info('[5/5] verifying: a restore is done when pulumi can log in to what it restored')
    restored = state.stacks(target)
    if not restored:
        log.error('%s serves no stacks after the restore, so the state did not arrive', state.endpoint(target.url))
        return 1
    log.info('the restored backend serves %d stack(s): %s', len(restored), ', '.join(restored))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == 'census' and args.yes and not (args.prune_superseded or args.prune_expired):
        parser.error('--yes deletes what --prune-superseded or --prune-expired lists, and is nothing without one')

    # One dispatch, and the kit is opened by the arms that need it rather than
    # before them: `pins`, `ssh`, `census`, `release-lock` and a drill's `restore
    # --identity-file` run on machines that have no offline database, and
    # asking for one would fail before the command started.
    def kit() -> KdbxStore:
        return KdbxStore.from_env(args.kdbx)

    def registry() -> escrow.Registry:
        return escrow.Registry.open(args.escrow)

    try:
        match args.action:
            case 'pins':
                return 0 if provision.verify_pins() else 1
            case 'ssh':
                provision.ssh(args.command)
            case 'census':
                return census.run(
                    state.connection(args.bundle),
                    prune=args.prune_superseded,
                    expire=args.prune_expired,
                    ceiling=args.ceiling,
                    yes=args.yes,
                    out=sys.stdout,
                )
            case 'release-lock':
                return locks.release(
                    state.connection(args.bundle),
                    stack=args.stack,
                    start=locks.read_start(args.start_file),
                    hostname=socket.gethostname(),
                )
            case 'restore' if args.identity_file is not None:
                return _restore(
                    None,
                    registry=registry(),
                    bundle_dir=args.bundle,
                    source=args.dump,
                    identity=args.identity_file,
                    force=args.force,
                )
            case 'render':
                store = kit()
                print(
                    render.render_ignition(
                        config.machine(
                            config.Roots.recover(escrow.Vault.open(store, registry())),
                            address=args.address,
                            dump_key_id='rendered-without-a-key',
                            dump_key='rendered-without-a-key',
                            bucket_id='rendered-without-a-bucket',
                        )
                    )
                )
                return 0
            case 'bundle':
                store = kit()
                config.write_client_bundle(
                    config.client_bundle(
                        pki.Authority.from_pem(escrow.Vault.open(store, registry()).recover(escrow.CA)),
                        name=args.name,
                        address=args.address,
                    ),
                    args.directory,
                )
                return 0
            case 'dump':
                return _dump(kit(), registry=registry(), bundle_dir=args.bundle, output=args.output)
            case 'restore':
                return _restore(
                    kit(),
                    registry=registry(),
                    bundle_dir=args.bundle,
                    source=args.dump,
                    identity=None,
                    force=args.force,
                )
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled action {args.action}')
    except REFUSALS as exc:
        log.error('%s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
