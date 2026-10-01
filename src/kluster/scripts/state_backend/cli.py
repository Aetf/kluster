"""`state-backend` -- inspect, dump, restore, adopt and probe the appliance.

The appliance itself is the `state-backend` stack's, created, converged and
replaced by `operator-stack state-backend up` (physical/state-backend.md §1);
this command keeps what declares nothing (rfc-006 §4.5). `dump` and `restore`
are the standalone moves of the playbooks (physical/state-backend.md §7), each
of which verifies rather than reports, and the restore a run of the stack
owes after a replacement it took no dump for. `render` renders a scratch box
for the rebuild drill, `ssh` logs in for diagnosis, `bundle` writes a client
bundle, and `pins` checks the pinned artifacts. `adopt` is the cutover's one
write: the ids of what already exists, into the stack's configuration.
`probe` is the one command written to run somewhere other than a workstation:
the scheduled checks on the certificate and the dumps (`probe.py`).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from kluster import conventions
from kluster.conventions import CompartmentMissing
from kluster.lib import stack_environment
from kluster.lib.pulumi_cli import PulumiRefused
from kluster.lib.state_backend import adoption, committed, render, settings, state
from kluster.lib.state_backend.state import StateError
from kluster.scripts.credentials import escrow, pki, workstation
from kluster.scripts.credentials.age import AgeError
from kluster.scripts.credentials.escrow import EscrowError
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.credentials.workstation import WorkstationError

from . import adopt, config, probe, provision

log = logging.getLogger(__name__)

#: What `main` turns into one line and an exit status of 1: every refusal a
#: module this program imports can raise. A refusal is a decision with the
#: repair in its message, and an operator reads a traceback as a crash instead,
#: with that repair buried under the stack. The census is the import closure
#: rather than today's call paths, because that is the boundary a test can
#: hold (`test_provision.py` walks it in both directions); the members no
#: current call reaches -- `PulumiRefused`, which `state.stacks` and the
#: configuration's reads translate into their own, and `CompartmentMissing`,
#: which `conventions` raises for a compartment it does not record -- are here
#: so that a call that reaches one tomorrow gets the line rather than the
#: traceback.
REFUSALS = (
    AgeError,
    CompartmentMissing,
    CredentialRejected,
    EscrowError,
    KdbxError,
    PulumiRefused,
    SlotRefused,
    StateError,
    WorkstationError,
    adopt.Refused,
    adoption.Refused,
    committed.Refused,
    stack_environment.EnvironmentRefused,
)


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

    _ = actions.add_parser(
        'adopt',
        help="write the ids of the appliance's existing resources into the state-backend stack's configuration",
        description=(
            'Look up every resource the state-backend stack keeps but the box, the image and the dump key -- '
            'the VCN, its gateway and subnet, the reserved address, the image bucket and the dump bucket -- '
            "under the names the stack declares, signed with the stack's own OCI key and B2 "
            'management key, and write their ids into its configuration in the clear. The stack imports each '
            "through its program on its next run. Run once, before the cutover's first plan; it writes "
            'nothing to OCI or B2.'
        ),
    )

    # What the ops repository's scheduled workflow runs (state-backend.md §6):
    # every probe unless one is named, each verdict printed, the exit status
    # saying which failed. Needs no offline database: the certificate probe
    # is a handshake and the dump-age probe reads its key from the
    # environment, which is where a workflow secret arrives.
    probing = actions.add_parser(
        'probe',
        help='check the server certificate and the age of the newest dump from outside the box',
        description=(
            f'The two scheduled probes. `{probe.CERTIFICATE}` reads the server certificate off a TLS handshake '
            f'with {settings.ADDRESS}:{settings.PORT} and fails when it is not valid, has less than '
            f'{config.EXPIRY_ALERT_MARGIN.days} days left, or does not name the address; `{probe.DUMPS}` lists '
            f'the dump prefix as the list-only key in {probe.KEY_ID_ENV} and {probe.KEY_ENV} and fails when the '
            f'newest dump is older than {probe.hours(settings.DUMP_MAX_AGE)} or there is none. Every verdict is printed, '
            'with the playbook a failure calls for.'
        ),
        epilog=(
            f'exit status: 0 every probe passed; {probe.FAILED[probe.CERTIFICATE]} the certificate probe failed; '
            f'{probe.FAILED[probe.DUMPS]} the dump-age probe failed; their sum when both did; '
            '1 the run could not probe at all — read its last words.'
        ),
    )
    _ = probing.add_argument(
        '--only',
        choices=probe.PROBES,
        default=None,
        help='run one probe alone',
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
    args = build_parser().parse_args(argv)

    # One dispatch, and the kit is opened by the arms that need it rather than
    # before them: `pins`, `ssh`, `probe`, `adopt` and a drill's `restore
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
            case 'probe':
                return probe.run(only=args.only, environ=os.environ)
            case 'ssh':
                provision.ssh(args.command)
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
            case 'adopt':
                configuration = adopt.stack(workstation.repo_root())
                _ = adopt.adopt(
                    configuration,
                    adopt.clients(configuration),
                    conventions.OCI_TENANCY.compartments[conventions.STATE_BACKEND].require(),
                )
                return 0
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled action {args.action}')
    except REFUSALS as exc:
        log.error('%s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
