"""`state-backend` — provision, inspect, re-provision, dump, restore and probe the appliance.

`provision` is idempotent end to end: it is equally the bring-up command and
the re-provision command, which is what keeps the rebuild path warm. It is not
therefore harmless — replacing the box destroys every stack's state that is
not in a dump — so a run that finds drift on a box that exists reports it and
stops, and `--force` is how a replacement is asked for. A run that leaves the
box standing, matching or not, writes nothing to OCI or B2 but to point the
reserved address back at the box: every other such write belongs to a run
that launches a box. `dump` and `restore`
are the other half of that path: every playbook that replaces the box is a
dump, a provision and a restore (physical/state-backend.md §7), with the dump
taken by the converge itself, and each of them verifies rather than reports.
A replacement records the restore it leaves owed in the workstation slot, and
`provision` does not exit 0 while that record stands; a restore over the
slot's bundle is what clears it.
`probe` is the one command written to run somewhere other than a workstation:
the scheduled checks on the certificate and the dumps (`probe.py`).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from kluster.conventions import CompartmentMissing
from kluster.scripts.credentials import b2, entries, escrow, pki, workstation
from kluster.scripts.credentials.age import AgeError
from kluster.scripts.credentials.escrow import EscrowError
from kluster.scripts.credentials.kdbx import KdbxError, KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected
from kluster.scripts.credentials.oci_slot import SlotUnusable
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.credentials.workstation import WorkstationError

from . import config, probe, provision, settings, state
from .state import StateError

log = logging.getLogger(__name__)

#: What `provision` exits with when a restore a replacement left owed has not
#: run over this checkout's bundle -- this run's, or an earlier one's
#: (`RESTORE_OWED`). Neither 0 — 5432 answering over an empty database is not the end
#: of the operation, and a caller that stops reading at the exit code would
#: otherwise be told the appliance is ready — nor 1, which says that the run
#: failed and nothing more: a run can fail before it touches anything, or
#: after it has already destroyed the box, and which it was is in the run's
#: last words rather than in its status. This one means the same thing however
#: far the run got, which is what a caller can branch on. A wrapper reads it, so it is
#: published rather than internal: `provision --help` and
#: deploy/state-backend/README.md both name it.
RESTORE_PENDING = 3

#: The file in the workstation slot (`workstation.bundle_dir`) that records a
#: restore a replacement left owed: one line naming the dump that restore
#: takes, or an empty one for a box destroyed without a dump, whose state is
#: then the newest object in B2. Written as a run starts destroying the box,
#: because from then on the state is out of it whichever way the run ends, and
#: removed by a `restore` over the same slot's bundle, the one thing that puts
#: the state back. While it stands, a `provision` that would otherwise exit 0
#: exits `RESTORE_PENDING`, naming that dump again: a re-run after a
#: replacement that stopped part way launches or finds a box answering over an
#: empty database, and nothing else the run reads tells that box from one
#: holding its state.
#:
#: It is this checkout's record rather than a fact about the box: a restore
#: run from another checkout leaves it standing here, and the words that name
#: it say so.
RESTORE_OWED = 'restore-owed'

#: What `main` turns into one line and an exit status of 1: every refusal a
#: module this program imports can raise. A refusal is a decision with the
#: repair in its message, and an operator reads a traceback as a crash instead,
#: with that repair buried under the stack. The census is the import closure
#: rather than today's call paths, because that is the boundary a test can
#: hold (`test_provision.py` walks it in both directions); the member no
#: current call reaches — `SlotRefused`, which `state.stacks` translates into a
#: `StateError` — is here so that a call that reaches it tomorrow gets the line
#: rather than the traceback.
REFUSALS = (
    AgeError,
    CompartmentMissing,
    CredentialRejected,
    EscrowError,
    KdbxError,
    SlotRefused,
    SlotUnusable,
    StateError,
    WorkstationError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='state-backend', description=__doc__)
    _ = parser.add_argument('--kdbx', type=Path, default=None, help='the cluster KeePassXC database')
    _ = parser.add_argument(
        '--seed-entry',
        default=entries.SEEDS['b2'].entry,
        help='entry holding the B2 seed key',
    )
    _ = parser.add_argument(
        '--escrow',
        type=Path,
        default=None,
        help=f'the escrow registry (default: the {escrow.DIRECTORY}/ directory of this checkout)',
    )
    _ = parser.add_argument(
        '--compartment',
        default=None,
        help='OCI compartment (default: the one `conventions` names for the appliance)',
    )
    actions = parser.add_subparsers(dest='action', required=True)

    render = actions.add_parser('render', help='render the Ignition config without touching the cloud')
    _ = render.add_argument('--address', default='192.0.2.10', help='address to issue the server certificate for')

    provision_cmd = actions.add_parser(
        'provision',
        help='create the appliance, or compare the running one to this commit and replace it when asked',
        epilog=(
            'exit status: 0 the appliance is current and no restore is owed; '
            f'{RESTORE_PENDING} a replacement from this checkout left a restore owed that has not run over its '
            'bundle, so run `state-backend restore` with the dump this printed; '
            '1 the run failed — read its last words for whether it had already replaced the box, '
            'which is where the dump to restore is named.'
        ),
    )
    # Re-provision is the appliance's only apply path (state-backend.md §1),
    # and it is destructive by construction: a new box, a new dump key, and
    # whatever state was not in the last dump. `provision` decides for itself
    # whether that is needed by comparing the box to the commit; this flag is
    # for the case with no diff to find -- rotating the dump key, or replacing
    # a box that is broken in some way the metadata cannot show.
    _ = provision_cmd.add_argument(
        '--replace',
        action='store_true',
        help='rebuild even if the box already matches (rotates the dump key with it)',
    )
    # Drift is a reason to replace, not permission to, so the plain converge
    # reports what it found and stops and this is how an operator asks for
    # exactly that replacement. `--force` rather than `--yes` because `restore`
    # spells the same concept that way already, and because `--yes` elsewhere
    # -- `pulumi up --yes` -- promises to skip a prompt, which is a promise
    # this cannot keep: there is no prompt to skip.
    _ = provision_cmd.add_argument(
        '--force',
        action='store_true',
        help='replace the box that is running (the plain converge reports and stops)',
    )
    # The escape for a box that cannot answer at all: an unreachable machine or
    # a Postgres that will not start is the case the rebuild path is the
    # diagnosis for (state-backend.md §6), where refusing to replace without a
    # dump would leave nothing to do.
    _ = provision_cmd.add_argument(
        '--no-dump',
        action='store_true',
        help='replace without dumping first, accepting the loss of everything since the last nightly dump',
    )
    # The dump lands wherever the command was run unless this says otherwise,
    # and `provision` is run from the checkout: `.gitignore` covers the default
    # name so that a dump of every stack's state cannot be committed to a
    # public repository by the next `jj` command.
    _ = provision_cmd.add_argument(
        '--dump-output',
        type=Path,
        default=None,
        help=f'where to write that dump (default: ./{settings.NAME}-<UTC stamp>.dump.age)',
    )
    _ = provision_cmd.add_argument(
        '--bundle',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client bundle to take that dump over',
    )

    # Diagnosis only: the box is never configured by hand (state-backend.md
    # §1). Needs no offline database, so it does not ask for one.
    ssh_cmd = actions.add_parser('ssh', help='log in to the appliance for diagnosis')
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

    restore = actions.add_parser('restore', help='feed a dump into a provisioned box')
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


def _rebuild_reasons(
    roots: config.Roots,
    session: b2.Session,
    existing: object,
    *,
    address: str,
    bucket_id: str,
    replace: bool,
) -> list[str]:
    """Why the running box is not the box this commit describes.

    Asked only of a box that exists, so an empty list means one thing: this
    one matches and nothing has to happen. Provision applies the current
    commit, so anything the repository changed -- the Butane file, an operator
    key, a pinned image, the address the server certificate is issued for --
    makes the box stale in exactly the same way, and the dump key is one
    component among them rather than a special case.

    The comparison is per component (config.digests), so the reason a box is
    being replaced names what changed instead of asserting that something did.

    One reason is not a comparison against the repository at all: the server
    certificate's remaining life. Every digested component is re-derived here
    and is therefore always young, so an expiry the box is walking towards is
    invisible to equality; what the box records is when its own certificate
    dies, and `config.renewal_due` reads that against the clock.

    Every input is read from OCI or B2 before the run has decided to write to
    either.
    """
    if replace:
        return ['--replace was asked for']

    recorded = provision.instance_config(existing)
    reasons: list[str] = []
    if not b2.dump_key_is_current(session, recorded.dump_key_id, bucket_id=bucket_id):
        # The box cannot be handed a new key without being rebuilt: the
        # secret only exists inside the Ignition it booted with.
        held = recorded.dump_key_id or 'none recorded'
        reasons.append(f'the dump key the box holds ({held}) is not the intended one')

    intended = config.digests(roots, address=address, dump_key_id=recorded.dump_key_id, bucket_id=bucket_id)
    changed = config.drift(intended, recorded.digests)
    if changed and not recorded.digests:
        reasons.append('the box predates this bookkeeping, so what it was built from cannot be compared')
    elif changed:
        reasons.append(f'the machine definition changed: {", ".join(changed)}')
    expiring = config.renewal_due(recorded.server_cert_expiry)
    if expiring is not None:
        reasons.append(expiring)
    return reasons


def _missing_inputs(reserved: provision.ReservedAddress | None, bucket: b2.Bucket | None) -> list[str]:
    """Why a running box cannot be compared at all: what the comparison reads is missing.

    The address and the bucket are inputs of the box's bill of materials, so
    a box standing without either is not one this commit describes; its
    replacement is also what creates them.
    """
    reasons: list[str] = []
    if reserved is None:
        reasons.append('no reserved address carries the appliance name')
    if bucket is None:
        reasons.append(f'the bucket its dumps go to, {settings.B2_BUCKET}, does not exist')
    return reasons


@dataclass(frozen=True)
class Groundwork:
    """What a new box stands on that does not depend on the box it replaces.

    Converged before the dump and the terminate, because every piece of it can
    fail and none of it needs the old box gone: a failure here stops the run
    with the old box still serving. The image import is the long piece.
    """

    bucket_id: str
    placement: provision.Placement
    nsg_id: str
    reserved: provision.ReservedAddress
    image_id: str
    availability_domain: str


def _groundwork(clients: provision.OciClients, session: b2.Session, found: provision.Survey) -> Groundwork:
    """Create or converge everything the launch needs, short of the dump key."""
    log.info('converging bucket %s', settings.B2_BUCKET)
    bucket_id = b2.ensure_bucket(
        session,
        settings.B2_BUCKET,
        prefix=settings.B2_PREFIX,
        retention_days=settings.B2_RETENTION_DAYS,
    )
    log.info('converging the OCI network: VCN, subnet, gateway, security group, reserved address')
    placement = provision.ensure_network(clients, found)
    nsg_id = provision.ensure_security_group(clients, placement.vcn_id, found)
    reserved = provision.ensure_reserved_ip(clients, found)
    log.info('converging the custom image — a release not imported yet takes ten minutes and more')
    image_id = provision.ensure_image(clients, found)
    return Groundwork(
        bucket_id=bucket_id,
        placement=placement,
        nsg_id=nsg_id,
        reserved=reserved,
        image_id=image_id,
        availability_domain=provision.shape_availability_domain(clients, image_id),
    )


@dataclass(frozen=True)
class LaunchedBox:
    """The box a launch created, and the SSH identity it was built with.

    The pin travels with the instance id because it is only worth anything as
    the public half of the key that same render delivered: carried apart, the
    file this run writes could describe a different box.
    """

    instance_id: str
    host_public_key: str


def _launch_box(
    clients: provision.OciClients,
    roots: config.Roots,
    *,
    dump_key: b2.AppKey,
    ground: Groundwork,
) -> LaunchedBox:
    """Render this commit's machine around `dump_key` and launch it.

    The push half of the dump key's delivery: the key's only consumer is the
    box, and the box comes into being holding it, because B2 discloses an
    application key's secret once and a box launched without it can never be
    handed one afterwards.
    """
    address = ground.reserved.address
    log.info('rendering the Ignition config for %s', address)
    # One machine, rendered once: three facts about the box have to come from
    # the same render. The Ignition it boots with, the expiry recorded beside
    # it, and the SSH host key recorded beside that -- a second
    # `config.machine` call would issue a second certificate and mint a second
    # host key, and the box would be pinned to a key it never held.
    built = config.machine(
        roots,
        address=address,
        dump_key_id=dump_key.key_id,
        dump_key=dump_key.key,
        bucket_id=ground.bucket_id,
    )
    ignition = config.render_ignition(built)
    host_public_key = config.host_public_key(built)
    log.info('launching the instance')
    instance_id = provision.ensure_instance(
        clients,
        subnet_id=ground.placement.subnet_id,
        nsg_id=ground.nsg_id,
        image_id=ground.image_id,
        availability_domain=ground.availability_domain,
        ignition=ignition,
        digests=config.digests(roots, address=address, dump_key_id=dump_key.key_id, bucket_id=ground.bucket_id),
        dump_key_id=dump_key.key_id,
        server_cert_expiry=config.expires_at(built),
        ssh_host_key_pub=host_public_key,
    )
    return LaunchedBox(instance_id=instance_id, host_public_key=host_public_key)


def _hand_over(roots: config.Roots, *, address: str, launched: LaunchedBox | None) -> bool:
    """Write what the workstation needs to reach the box, then wait for it to answer.

    Local writes only: the operator's client bundle, and the host-key pin of a
    box this run built. A run that launched nothing writes no pin -- the box
    did not change, and `ssh` re-reads the pin from the instance's metadata on
    every exec regardless.
    """
    slot = workstation.bundle_dir()
    config.write_client_bundle(config.client_bundle(roots.ca, name='operator', address=address), slot)
    log.info('operator certificate bundle written to %s', slot)
    if launched is not None:
        # Placed where `state-backend ssh` reads it, so the first diagnosis
        # after a replace needs no fetch of its own.
        known_hosts = config.write_known_hosts(slot, address=address, public_key=launched.host_public_key)
        log.info('host key pin for %s written to %s', address, known_hosts)

    if not provision.wait_for_backend(address):
        log.error('the backend did not answer on %s:%d — ssh core@%s to look', address, settings.PORT, address)
        return False
    log.info('backend answering on %s:%d', address, settings.PORT)
    return True


def _provision(
    store: KdbxStore,
    *,
    seed_entry: str,
    compartment: str | None,
    replace: bool,
    force: bool,
    dump: bool,
    dump_output: Path | None,
    bundle_dir: Path,
    registry: escrow.Registry,
) -> int:
    # Each stage says what it is starting, not only what it finished: the
    # image import and the first boot are minutes-long, and a log that only
    # speaks on success is indistinguishable from a hang while they run.
    log.info('[1/6] authorizing with OCI, and looking for a box that already exists')
    clients = provision.OciClients.load(compartment)
    # Every adopt-by-name read, before the run's first write: what the stages
    # below adopt is decided here, and each of them creates only what the
    # survey answered None for.
    found = provision.survey(clients)
    existing = found.instance

    # Ahead of everything else because whether a box is running is what
    # decides whether this run may generate roots at all: on a live appliance,
    # a label the escrow cannot answer for means the wrong escrow rather than
    # a first run (config.Roots.ensure).
    log.info('[2/6] opening the escrow with the kit')
    roots = config.Roots.ensure(escrow.Vault.open(store, registry), appliance_exists=existing is not None)

    log.info('[3/6] authorizing with B2, and looking up bucket %s', settings.B2_BUCKET)
    session = b2.Session.from_entry(store, seed_entry)
    # Before every write this run makes to either provider, the bucket and the
    # terminate included: a seed for another account would create the bucket there. The
    # mint checks again for its own sake; this one is what makes the refusal
    # cost nothing on this path.
    b2.verify_account(session.account_id)
    bucket = next(iter(session.buckets(settings.B2_BUCKET)), None)
    reserved = provision.found_reserved_ip(clients, found)
    # A restore an earlier replacement left owed, read before this run can
    # write one of its own: every way this run ends reads it from here.
    owed = _owed()

    # Up to here the run has written to neither provider, and a run that leaves
    # the box standing -- because it matches, or because its drift was not
    # asked to be acted on -- writes nothing to either but the one repair below.
    log.info('[4/6] comparing the running box against this commit')
    if existing is not None:
        if reserved is None or bucket is None:
            reasons = _missing_inputs(reserved, bucket)
        else:
            reasons = _rebuild_reasons(
                roots, session, existing, address=reserved.address, bucket_id=bucket.bucket_id, replace=replace
            )
            if not reasons:
                log.info('appliance %s matches the repository; nothing to change', existing.id)
                # The one write a run over a standing box can make, and only
                # when the reservation points elsewhere: a run that stopped
                # between a launch and the attach leaves the new box with no
                # public address, and this is the way back to it.
                provision.attach_reserved_ip(clients, instance_id=str(existing.id), public_ip_id=reserved.id)
                return _settled(owed) if _hand_over(roots, address=reserved.address, launched=None) else 1
        for reason in reasons:
            log.warning('%s', reason)
        if not (replace or force):
            log.error('%s would be replaced, and nothing has been changed', existing.id)
            log.error(
                "a replacement destroys the box holding every stack's state, and its boot volume with it: "
                're-run with --force to replace this one, or --replace to rebuild a box that matches'
            )
            return 1

    # Everything fallible that does not need the old box gone, the image
    # import above all, runs while that box still serves: a failure here ends
    # the run with nothing destroyed.
    log.info('[5/6] converging what the new box stands on, before anything is destroyed')
    ground = _groundwork(clients, session, found)

    #: The box this run launched, once it is running.
    launched: LaunchedBox | None = None
    answered = False
    # Whether the dump key's predecessor was retired, which the launch is
    # followed by and which can raise with the new box already running.
    retired = False
    # Whether a successor was minted: only then can the predecessor be the
    # one left live, by a retirement that failed or a launch whose wait was lost.
    minted = False
    # The key the box being replaced holds: the predecessor that retirement
    # is for, named by the closing instruction when it stays live.
    superseded = provision.instance_config(existing).dump_key_id if existing is not None else ''
    # Set the moment the old box starts going away, not when the decision is
    # made: everything after that point owes the operator the closing
    # instruction, including the paths that raise. The `finally` below is what
    # makes that true of every exit, which is what lets the README promise
    # that silence means the old box is still serving.
    destroyed = False
    try:
        if existing is not None:
            if dump:
                taken = _dump_before_replacing(roots, output=dump_output, bundle_dir=bundle_dir, owed=owed)
                if taken is None:
                    return 1
                # A box an earlier replacement left empty dumps once anything
                # has opened it, and holds no stack: the record keeps the
                # dump the state is in unless this box serves one. A box that
                # does not answer the question is not read as empty.
                if owed is None:
                    owed = str(taken)
                else:
                    try:
                        serving = state.stacks(state.connection(bundle_dir))
                    except StateError as exc:
                        log.error(
                            'the box answered the dump but not `pulumi stack ls` (%s), so whether %s or %s holds the '
                            'state cannot be told; nothing has been destroyed, and a re-run asks again',
                            exc,
                            taken,
                            owed or 'the newest object in B2',
                        )
                        return 1
                    if serving:
                        owed = str(taken)
                    else:
                        log.warning(
                            '%s holds no stack, so the restore still owed takes %s',
                            taken,
                            owed or 'the newest object in B2',
                        )
            elif owed is None:
                log.warning('--no-dump: replacing without a dump, so everything since the nightly one is lost')
                owed = ''
            else:
                # The record says an earlier replacement left this box empty;
                # it cannot say whether the state went back some other way.
                log.warning('--no-dump: replacing without a dump; %s', _both_readings(owed))
            log.warning('replacing %s — 5432 goes away until the new box answers', existing.id)
            _owe(owed)
            destroyed = True
            provision.terminate_instance(clients, str(existing.id))
        # After the terminate comes what needs the old box gone or the new one
        # up -- the launch, which would otherwise adopt the old box by its
        # name, then the retirement of the dump key's predecessor, the
        # address pointed at the new box and the wait for it to answer -- and
        # two steps that need neither: the mint, on the branch that launches
        # because B2 discloses a key's secret once, and the render that
        # carries the key. Those two follow the terminate so that a run whose
        # dump fails has minted no key that nothing holds, and they are the
        # fallible steps left in the stretch with no backend.
        log.info('[6/6] minting the dump key the new box will hold, and launching it')
        pending = b2.mint_dump_key(session, bucket_id=ground.bucket_id)
        minted = True

        # Recorded the moment the launch returns, inside the push: the
        # predecessor's retirement follows it and can raise, and a box that
        # is running by then is what the closing instruction has to name.
        def launch(dump_key: b2.AppKey) -> LaunchedBox:
            nonlocal launched
            launched = _launch_box(clients, roots, dump_key=dump_key, ground=ground)
            return launched

        # Launching the box is this credential's push, so it runs through
        # `deliver` and the predecessor is retired only once the box holding
        # the successor exists -- the order every mint in that package has
        # (`credentials/delivery.py`).
        _, box = pending.deliver(launch)
        retired = True
        provision.attach_reserved_ip(clients, instance_id=box.instance_id, public_ip_id=ground.reserved.id)
        answered = _hand_over(roots, address=ground.reserved.address, launched=box)
        if not answered:
            return 1
        return RESTORE_PENDING if destroyed else _settled(owed)
    finally:
        # The stretch above is minutes long -- a launch, a first boot -- and
        # every step of it can raise: B2 refusing the mint, OCI refusing the
        # launch. Once the box is going away the state is in one file, so a
        # run that leaves this way says so before the traceback does.
        if destroyed:
            _restore_pending(
                owed or '',
                launched=launched,
                answered=answered,
                live_key=None if retired or not minted else superseded,
            )


def _owed_path() -> Path:
    return workstation.bundle_dir() / RESTORE_OWED


def _owed() -> str | None:
    """The restore a replacement left owed (`RESTORE_OWED`), or None when none is.

    The answer is the dump that restore takes, or '' for a box destroyed
    without one.
    """
    try:
        return _owed_path().read_text().strip()
    except FileNotFoundError:
        return None


def _owe(dump: str) -> None:
    """Record the restore this run's replacement leaves owed, before the terminate."""
    _ = workstation.write(_owed_path(), dump)


def _both_readings(owed: str) -> str:
    """What `--no-dump` costs over a box this checkout records a restore owed for, on each reading.

    The record cannot tell the box an earlier replacement left empty from one
    whose state went back from another checkout, so the words that send an
    operator to `--no-dump` carry both, and the file to delete on the second.
    """
    return (
        f'this checkout records a restore a replacement left owed ({_owed_path()}), of '
        f'{owed or "the newest object in B2"}. If this box is the one that replacement left empty, --no-dump '
        'replaces it and loses nothing of that, and the restore afterwards takes that dump. If it serves state '
        'that went back some other way -- a restore from another checkout -- delete that file first: --no-dump '
        'then loses what is not in the nightly object, and the restore afterwards takes the newest object in B2 '
        'rather than that dump'
    )


def _name_the_restore(owed: str) -> None:
    """The command that puts the state back, as the owed record names its dump."""
    if owed:
        log.warning('    state-backend restore %s', owed)
    else:
        log.warning('the box was destroyed without a dump, so the state is the newest object in B2:')
        log.warning('    state-backend restore <that object>')


def _settled(owed: str | None) -> int:
    """What a run that answered and destroyed nothing exits with.

    0 when no restore is owed. Otherwise the box it found or launched is the
    one an earlier replacement left empty -- or, where the record is stale,
    one whose state came back some other way -- and the run says which dump
    that restore takes and exits as the replacement did.
    """
    if owed is None:
        return 0
    log.warning(
        'the box answers, and this checkout records a restore still owed: a replacement run from here took the '
        'state out, and no restore over this bundle has put it back since:'
    )
    _name_the_restore(owed)
    log.warning(
        '%s records that restore as owed, and the restore removes it. If the box already holds its state -- '
        'restored from another checkout or bundle, or never destroyed because the terminate failed -- delete '
        'that file instead',
        _owed_path(),
    )
    return RESTORE_PENDING


def _restore_pending(owed: str, *, launched: LaunchedBox | None, answered: bool, live_key: str | None) -> None:
    """The last words of a run that destroyed the box: the state is not back yet.

    Said from how far the run got, because that decides the operator's next
    move. A run that saw no new box running cannot say whether one exists --
    a launch OCI accepted may still come up -- and a restore with nothing to
    go into fails, so it names the provision that brings a box up where none
    is and points the address at one that is. A box that has not answered
    cannot take the restore yet. A box that answers serves an empty database
    -- it initdb'd a fresh data directory -- and a `pulumi` run against it
    reads a backend serving nothing and acts on that.

    The re-run is named with the commit it runs from, because a box this run
    launched was built from this one: from another, the re-run reads that box
    as drifted and stops, and a replacement of it has no state to dump.

    `live_key` is the dump key the destroyed box held, when a successor was
    minted and the retirement has not run -- '' when that box recorded none
    -- and None otherwise. Beside a new box that is running, the retirement
    is what failed. With none seen, the re-run launches one and retires it
    then -- unless OCI accepted a launch whose wait was lost, and the re-run
    finds that box and launches nothing, so the words say both.
    """
    if launched is None:
        log.warning(
            'no new box was seen running: re-run `state-backend provision` from this commit, which brings a box '
            'up where none is and points the address at one that is. If the terminate is what failed, the old box '
            'may still be standing or still going away; one still standing holds its state and is owed no restore: '
            "a re-run that finds it matching the commit exits 3 on this checkout's record, and deleting that record "
            'is the step then; one that finds it drifted stops as before the replacement, and `--force` dumps it '
            'afresh. Otherwise, once a box answers:'
        )
    elif not answered:
        log.warning(
            'the new box %s has not answered, so it cannot take the restore yet: re-run '
            '`state-backend provision` from this commit, which points the address at it and waits again -- '
            '`state-backend ssh` reaches it once the address does; once it answers:',
            launched.instance_id,
        )
    else:
        log.warning('the new box %s serves an empty database until the state goes back into it:', launched.instance_id)
    _name_the_restore(owed)
    if launched is None and live_key is not None:
        log.warning(
            'if the re-run finds a box this run launched rather than launching one, the dump key the destroyed box '
            'held%s stays live: it can write into the dump prefix and nothing else, and the next run that launches '
            'a box retires it -- `state-backend provision --replace` once the restore is done',
            f' ({live_key})' if live_key else '',
        )
    elif live_key is not None:
        log.warning(
            'retiring the dump key the destroyed box held%s failed, so it is still live: it can write into the '
            'dump prefix and nothing else, and the next run that launches a box retires it with every other '
            'superseded dump key -- `state-backend provision --replace` once the restore is done',
            f' ({live_key})' if live_key else '',
        )


def _dump_before_replacing(
    roots: config.Roots, *, output: Path | None, bundle_dir: Path, owed: str | None
) -> Path | None:
    """Dump the box about to be destroyed, or answer None having destroyed nothing.

    The nightly timer leaves a window of up to a day, and a rebuild throws
    away whatever is in it; this closes that window without the operator
    having to remember to. It is a precondition rather than best effort — a
    replacement whose dump did not happen is the loss the whole step exists to
    prevent — so a failure here stops the run with the box still standing.
    `--no-dump` is how an operator says the box cannot be dumped at all and
    the loss is accepted.

    The dump goes over a client bundle that authenticates against the box
    being replaced — by default the workstation slot, which holds exactly
    that: same CA, same address.

    A box an earlier replacement left empty (`owed`) names no table until
    something opens it; once the backend has created its empty table the
    dump passes and holds no stack, and the record keeps the earlier dump
    (`_provision`). Where its dump fails, the refusal says what `--no-dump`
    costs on both readings of the record, since the record cannot tell an
    empty box from one whose state went back from another checkout.
    """
    destination = (output if output is not None else Path(state.dump_name())).resolve()
    log.info('dumping the running box before it is replaced, into %s', destination)
    try:
        _write_dump(destination, bundle_dir=bundle_dir, recipients=roots.age_recipients)
    except StateError as exc:
        log.error('the dump of the running box failed: %s', exc)
        log.error('nothing has been destroyed; the box is still serving')
        if owed is None:
            log.error(
                'a missing or stale bundle is `state-backend bundle operator --address <ip>`; '
                '--no-dump replaces a box that cannot be dumped at all, and loses what is not in the nightly object'
            )
        else:
            log.error('%s', _both_readings(owed))
        return None
    return destination


def _refuse_to_overwrite(destination: Path) -> None:
    """A dump never lands on top of one.

    Asked twice: by `_dump` before it opens the escrow, so a name clash costs
    no kit password, and by the writer itself, which has a second caller.
    """
    if destination.exists():
        raise StateError(f'{destination} already exists; a dump never overwrites one')


def _write_dump(destination: Path, *, bundle_dir: Path, recipients: Sequence[str]) -> None:
    """`pg_dump -Fc` under age, verified before the file is called a dump.

    The single writer of the operator-side artifact, behind both commands that
    produce one — `state-backend dump`, and the converge dumping a box it is
    about to destroy — so the two cannot drift into producing different files.
    """
    _refuse_to_overwrite(destination)
    target = state.connection(bundle_dir)
    # The plaintext archive never lands beside the encrypted one: it is the
    # whole state in the clear, and it exists only for as long as the two
    # steps that read it. `TemporaryDirectory` makes it 0700.
    with tempfile.TemporaryDirectory(prefix=f'{settings.NAME}-') as tmp:
        archive = Path(tmp) / 'state.dump'
        log.info('dumping the live state over the client bundle in %s', bundle_dir)
        state.pg_dump(target, archive)
        log.info('verifying the archive before calling it a dump')
        _ = state.verify_dump(archive)
        log.info('encrypting the dump')
        state.encrypt(archive, destination, recipients)
    log.info(
        '%s holds %.1f MiB, readable by the %d recipient(s) the appliance encrypts to',
        destination,
        destination.stat().st_size / 2**20,
        len(recipients),
    )


def _dump(store: KdbxStore, *, registry: escrow.Registry, bundle_dir: Path, output: Path | None) -> int:
    """A dump on demand, in the form the appliance's own timer writes.

    Encrypted to the escrow's recipients rather than left in plain text, and
    listed before it is called a dump: an archive naming no table is a dump of
    a database that has lost its state — what a replaced box holds until
    anything opens it and the backend creates its empty table — and the operator taking one is usually about to destroy the box
    it came from (§7.2).
    """
    destination = (output if output is not None else Path(state.dump_name())).resolve()
    _refuse_to_overwrite(destination)

    log.info('[1/2] opening the escrow with the kit, for the recipients the appliance encrypts its dumps to')
    recipients = config.age_recipients(escrow.Vault.open(store, registry))
    log.info('[2/2] taking the dump')
    _write_dump(destination, bundle_dir=bundle_dir, recipients=recipients)
    return 0


def _served(target: state.Connection) -> list[str]:
    """The stacks the backend serves, or nothing if it cannot answer at all.

    Used before a restore, where a backend that refuses the question is the
    ordinary case: a box provisioned minutes ago has an empty database that
    no `pulumi` has ever written a layout into. So this only ever reports a
    positive answer, and the caller's guard only ever fires on one.
    """
    try:
        return state.stacks(target)
    except StateError as exc:
        log.info('the backend cannot list stacks yet (%s); a box provisioned minutes ago cannot either', exc)
        return []


def _restore(
    store: KdbxStore | None,
    *,
    registry: escrow.Registry,
    bundle_dir: Path,
    source: Path,
    identity: Path | None,
    force: bool,
) -> int:
    """Feed a dump into a provisioned box, and prove afterwards that it took.

    The order is the one that makes each failure cheap: refuse to overwrite a
    populated backend before anything is decrypted, verify the archive before
    it touches the database, restore in one transaction, and only then
    report — by asking `pulumi` what the backend now serves.
    """
    target = state.connection(bundle_dir)
    log.info('[1/5] asking the target backend what it already holds')
    occupied = _served(target)
    if occupied and not force:
        log.error('%s already serves %d stack(s): %s', state.endpoint(target.url), len(occupied), ', '.join(occupied))
        log.error('restoring over live state is `--force`; a rebuild restores into a box that has none')
        if (bundle_dir / RESTORE_OWED).exists():
            log.error(
                '%s records this restore as owed, and the backend already serves its stacks: the record is stale, '
                'and deleting it is the step, not --force',
                bundle_dir / RESTORE_OWED,
            )
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
    # The restore a replacement left owed, recorded beside the bundle it
    # would go over, is this one: the state is back, and `provision` may say 0
    # again (`RESTORE_OWED`).
    owed = bundle_dir / RESTORE_OWED
    if owed.exists():
        owed.unlink()
        log.info('the restore a replacement left owed is done; %s is gone', owed)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)

    # One dispatch, and the kit is opened by the arms that need it rather than
    # before them: `pins`, `ssh`, `probe` and a drill's `restore
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
                provision.ssh(provision.OciClients.load(args.compartment), args.command)
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
                    config.render_ignition(
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
            case 'provision':
                return _provision(
                    kit(),
                    seed_entry=args.seed_entry,
                    compartment=args.compartment,
                    replace=args.replace,
                    force=args.force,
                    dump=not args.no_dump,
                    dump_output=args.dump_output,
                    bundle_dir=args.bundle,
                    registry=registry(),
                )
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
