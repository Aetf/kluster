"""The OCI calls a recovery procedure makes outside the program, signed as `physical`'s own user.

A runbook that takes a step the program must not take -- the termination of
operations.md §3.5 step 3, which a `destroy` cannot make without reaching the
reserved IP -- or that reads what the cloud holds against what state says
makes these calls through `credentials derived oci-physical <verb>`. Three
verbs, each over OCIDs:

-   `terminate` an instance with its boot volume, wait for the work request
    OCI answers the termination with to end, and read the instance and its
    boot volume until both are `TERMINATED`;
-   `read` the state of an instance (with its shape), a public IP, a block
    volume or a boot volume, the kind told by the OCID's own resource type;
-   `detach` a block volume from the instance it is attached to, and wait for
    the attachment to read `DETACHED`.

**The credential is `physical`'s.** The user and the private key are read out
of that stack's configuration, where `credentials derived oci-physical mint`
writes them, with `physical`'s own passphrase from its workstation slot
(credentials.md §4.4), the one a run of `pulumi` by hand names. So the calls
can reach what the stack declares and nothing else: the key administers one
compartment. The key goes to the SDK in memory (`oci_iam.signing_config`),
reaches no file, and is in no line this module logs or prints, nor in the
repr of what holds it.

**What this cannot do is a capacity report.** `CreateComputeCapacityReport`
takes the tenancy's root compartment, which `physical`'s key cannot reach, so
operations.md §3.4's capacity reading stays with the operator's identity in
the console's Cloud Shell.

Every call is announced before it is made, and every wait says what it waits
for and how long it may take. A failure is never passed over: a service error
is refused with its status, code, message and request id, and a work request
that ends anywhere but `SUCCEEDED` is refused with its status and every error
OCI recorded on it.
"""

# The OCI SDK is generated and ships no type information; this module is a
# second piece of its untyped surface beside `oci_iam`, and everything it
# exports is annotated.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import oci

from kluster.lib import stack_environment

from ... import conventions
from . import derived, lifecycle, oci_iam, pulumi_config, workstation
from .masters import CredentialRejected

log = logging.getLogger(__name__)

#: The verbs, in the order the command line offers them.
VERBS = ('terminate', 'read', 'detach')

#: Appended to the SDK's own `User-Agent`, which names the SDK and its version:
#: a fixed string, so a request from here is told apart in an audit log by
#: what sent it and by nothing about who ran it.
USER_AGENT = 'kluster-credentials'

#: How long a termination's work request may take before the wait gives up.
#: OCI's own terminations of an A1 instance take a few minutes; the bound is a
#: stop rather than an expectation, and the CLI's own default.
TERMINATE_SECONDS = 1200
#: How long a detach's work request, or a resource's state after a work
#: request, may take before the wait gives up.
SETTLE_SECONDS = 600
#: The longest pause between two polls. The SDK's waiter starts at a second and
#: doubles up to this, so a short operation is seen ending promptly and a long
#: one is not polled every second.
POLL_SECONDS = 15

TERMINATED = 'TERMINATED'
DETACHED = 'DETACHED'
SUCCEEDED = 'SUCCEEDED'
#: The attachment states in which a volume is on an instance, or going onto one.
ATTACHING_OR_ATTACHED = frozenset({'ATTACHING', 'ATTACHED'})


class OciRefused(RuntimeError):
    """A call OCI refused, a work request that did not succeed, or a state other than the one a step ends in."""


@dataclass(frozen=True)
class Reading:
    """One resource as OCI reports it: the line a runbook's reading is taken from."""

    kind: str
    ocid: str
    #: The display name, or `-` for a resource OCI no longer answers for.
    name: str
    #: The lifecycle state, or `absent` where OCI answers that it holds no such resource.
    state: str
    #: What else the reading carries, as `key=value` words.
    details: tuple[str, ...] = ()

    def line(self) -> str:
        return ' '.join((self.kind, self.name, self.ocid, self.state, *self.details))


@dataclass(frozen=True)
class Session:
    """The SDK clients every verb uses, signing as one API key."""

    compute: Any
    blockstorage: Any
    network: Any
    work_requests: Any

    @classmethod
    def open(cls, key: oci_iam.ApiKey, *, endpoint: str | None = None) -> Session:
        """Clients for `key`, in this installation's region.

        `endpoint` addresses every service at one URL in place of the region's,
        which is how a test serves them; the compute, block storage, network and
        work request services share a version prefix, so one server answers all
        four.
        """
        config = oci_iam.signing_config(key.tenancy, key.user, key.private_key)
        config['additional_user_agent'] = USER_AGENT
        kwargs: dict[str, Any] = {'retry_strategy': oci.retry.DEFAULT_RETRY_STRATEGY}
        if endpoint is not None:
            kwargs['service_endpoint'] = endpoint
        return cls(
            compute=oci.core.ComputeClient(config, **kwargs),
            blockstorage=oci.core.BlockstorageClient(config, **kwargs),
            network=oci.core.VirtualNetworkClient(config, **kwargs),
            work_requests=oci.work_requests.WorkRequestClient(config, **kwargs),
        )


def physical_stack(bundle_dir: Path) -> pulumi_config.Stack:
    """`physical`'s configuration, opened with its passphrase from the workstation slot and no kit.

    The backend is the one the client bundle in `bundle_dir` names
    (`lifecycle.backend`); without one the read is refused before `pulumi`
    starts (`pulumi_config.Stack.require_backend`).
    """
    url, no_backend = lifecycle.backend(bundle_dir)
    return pulumi_config.Stack(
        name=pulumi_config.PHYSICAL,
        directory=pulumi_config.project_dir(),
        environment=pulumi_config.BackendEnvironment(
            url=url, no_backend=no_backend, bundle_dir=bundle_dir, physical=slot_passphrase
        ),
    )


def slot_passphrase() -> str:
    """`physical`'s passphrase from its workstation slot, refused with the command that fills a slot holding none."""
    found = workstation.physical_passphrase()
    if found is None:
        raise stack_environment.EnvironmentRefused(
            f'{workstation.physical_passphrase_path()} holds no passphrase: '
            f'`credentials derived {stack_environment.PHYSICAL_PASSPHRASE_ROW} recover` writes it, '
            'on a machine that holds the kit'
        )
    return found


def physical_key(stack: pulumi_config.Stack) -> oci_iam.ApiKey:
    """`physical`'s user and private key, read out of the stack's configuration.

    The tenancy is `conventions`', which every mint holds its seed to, and the
    fingerprint is the key's own (`oci_iam.ApiKey`), so the stored copy of it
    is not read. A value the stack does not hold is `pulumi`'s refusal
    (`pulumi_config.SlotRefused`); a value that is no key is refused here,
    naming the key and the command that writes it, and not the value.
    """
    log.info('reading the OCI user and API key from the %s stack configuration', stack.name)
    user = stack.get(derived.OCI_USER_KEY)
    private_key = stack.get(derived.OCI_PRIVATE_KEY_KEY)
    remedy = f'`credentials derived {derived.OCI_PHYSICAL_ROW} mint` writes both'
    if not user or not private_key:
        raise OciRefused(
            f'the {stack.name} stack configuration holds no {derived.OCI_USER_KEY} or no '
            f'{derived.OCI_PRIVATE_KEY_KEY}: {remedy}'
        )
    key = oci_iam.ApiKey(tenancy=conventions.OCI_TENANCY.tenancy_ocid, user=user, private_key=private_key)
    try:
        fingerprint = key.fingerprint
    except ValueError as exc:
        raise OciRefused(
            f'the {derived.OCI_PRIVATE_KEY_KEY} in the {stack.name} stack configuration is not a PEM private key: '
            f'{remedy}'
        ) from exc
    log.info('signing as %s, key %s', key.user, fingerprint)
    return key


def open_session(stack: pulumi_config.Stack) -> Session:
    """The clients for `physical`'s key, read out of `stack`."""
    try:
        return Session.open(physical_key(stack))
    except CredentialRejected as exc:
        raise OciRefused(str(exc)) from exc


def _call[T](what: str, call: Callable[[], T]) -> T:
    """Make one SDK call, refusing whatever it raises with what it was for.

    A service error keeps everything OCI said; a network failure (`OSError`,
    which the SDK's transport errors are) and a wait that ran out keep their
    own text. `what` is the phrase the refusal opens with.
    """
    try:
        return call()
    except oci.exceptions.CompositeOperationError as exc:
        cause: object = exc.cause
        if isinstance(cause, oci.exceptions.ServiceError):
            raise OciRefused(f'{what}: {_why(cause)}') from exc
        raise OciRefused(f'{what}: {cause}') from exc
    except oci.exceptions.ServiceError as exc:
        raise OciRefused(f'{what}: {_why(exc)}') from exc
    except oci.exceptions.MaximumWaitTimeExceeded as exc:
        raise OciRefused(f'{what}: the wait ran out ({exc})') from exc
    except OSError as exc:
        raise OciRefused(f'{what}: {exc}') from exc


def _why(exc: oci.exceptions.ServiceError) -> str:
    return f'HTTP {exc.status} {exc.code or "-"}: {exc.message} (opc-request-id {exc.request_id or "-"})'


def _absent(exc: oci.exceptions.ServiceError) -> bool:
    """Whether OCI answered that it holds no such resource, which is a reading rather than a failure."""
    return exc.status == 404


def _progress(what: str) -> Callable[[int, Any], None]:
    """The waiter's callback, logging each poll that found the wait not over."""

    def report(checks: int, response: Any) -> None:
        data = response.data
        status = getattr(data, 'status', None) or getattr(data, 'lifecycle_state', None)
        done = getattr(data, 'percent_complete', None)
        log.info('%s: still %s%s (poll %d)', what, status, f', {done:g}% complete' if done is not None else '', checks)

    return report


def _settle(what: str, client: Any, response: Any, state: str, *, seconds: int) -> Any:
    """Wait for the resource `response` read to reach `state`, or to be gone; the last response, or None if gone."""
    log.info('waiting for %s to read %s (at most %ds)', what, state, seconds)
    found = _call(
        f'waiting for {what} to read {state}',
        lambda: oci.wait_until(
            client,
            response,
            'lifecycle_state',
            state,
            max_interval_seconds=POLL_SECONDS,
            max_wait_seconds=seconds,
            succeed_on_not_found=True,
            wait_callback=_progress(what),
        ),
    )
    return None if found is oci.waiter.WAIT_RESOURCE_NOT_FOUND else found


def _work_request(session: Session, what: str, result: Any) -> None:
    """Refuse a work request that ended anywhere but `SUCCEEDED`, with every error OCI recorded on it.

    `result` is what a composite operation returns: the work request's last
    reading, or, where OCI answered the call with no work request, the call's
    own response, after which the resource's state is the only thing to wait
    on.
    """
    data = result.data
    if not isinstance(data, oci.work_requests.models.WorkRequest):
        log.warning('%s: OCI answered with no work request; its state is what is waited on', what)
        return
    if data.status == SUCCEEDED:
        log.info('%s: work request %s %s', what, data.id, data.status)
        return
    log.info('reading the errors work request %s recorded', data.id)
    errors = _call(
        f'reading the errors of work request {data.id}',
        lambda: oci.pagination.list_call_get_all_results(session.work_requests.list_work_request_errors, data.id).data,
    )
    recorded = '; '.join(f'{error.code}: {error.message}' for error in errors) or 'no error recorded on it'
    raise OciRefused(f'{what}: work request {data.id} ended {data.status}: {recorded}')


def _kind(ocid: str) -> str:
    """The resource type an OCID names: its second dotted field (`ocid1.<type>.<realm>...`)."""
    parts = ocid.split('.')
    return parts[1] if len(parts) > 3 and parts[0].startswith('ocid') else ''


def read(session: Session, ocid: str) -> list[Reading]:
    """The state of the instance, public IP, block volume or boot volume `ocid` names."""
    match _kind(ocid):
        case 'instance':
            return [_read_instance(session, ocid)]
        case 'publicip':
            return [_read_public_ip(session, ocid)]
        case 'volume':
            return [_read_volume(session, ocid)]
        case 'bootvolume':
            return [_read_boot_volume(session, ocid)]
        case kind:
            raise OciRefused(
                f'{ocid} names a {kind or "malformed"} resource; `read` takes an instance, a public IP, '
                'a volume or a boot volume'
            )


def _get(kind: str, ocid: str, call: Callable[[], Any]) -> Any:
    """One `get`, logged first; None where OCI holds no such resource."""
    log.info('reading %s %s', kind, ocid)
    try:
        return _call(f'reading {kind} {ocid}', call)
    except OciRefused as exc:
        if isinstance(exc.__cause__, oci.exceptions.ServiceError) and _absent(exc.__cause__):
            return None
        raise


def _read_instance(session: Session, ocid: str) -> Reading:
    found = _get('instance', ocid, lambda: session.compute.get_instance(ocid).data)
    if found is None:
        return Reading('instance', ocid, '-', 'absent')
    size = found.shape_config
    shape = (f'shape={found.shape}',)
    if size is not None:
        shape = (*shape, f'ocpus={size.ocpus:g}', f'memory-gb={size.memory_in_gbs:g}')
    return Reading('instance', ocid, found.display_name, found.lifecycle_state, shape)


def _read_public_ip(session: Session, ocid: str) -> Reading:
    found = _get('public IP', ocid, lambda: session.network.get_public_ip(ocid).data)
    if found is None:
        return Reading('public-ip', ocid, '-', 'absent')
    return Reading(
        'public-ip',
        ocid,
        found.display_name,
        found.lifecycle_state,
        (f'address={found.ip_address}', f'lifetime={found.lifetime}', f'assigned={found.assigned_entity_id or "-"}'),
    )


def _attachments(session: Session, volume: Any) -> list[Any]:
    """The attachments of `volume` that hold it on an instance, or are putting it on one."""
    log.info('listing the attachments of volume %s', volume.id)
    listed = _call(
        f'listing the attachments of volume {volume.id}',
        lambda: (
            oci.pagination.list_call_get_all_results(
                session.compute.list_volume_attachments, volume.compartment_id, volume_id=volume.id
            ).data
        ),
    )
    return [attachment for attachment in listed if attachment.lifecycle_state in ATTACHING_OR_ATTACHED]


def _read_volume(session: Session, ocid: str) -> Reading:
    found = _get('volume', ocid, lambda: session.blockstorage.get_volume(ocid).data)
    if found is None:
        return Reading('volume', ocid, '-', 'absent')
    on = ','.join(attachment.instance_id for attachment in _attachments(session, found)) or '-'
    return Reading('volume', ocid, found.display_name, found.lifecycle_state, (f'attached-to={on}',))


def _read_boot_volume(session: Session, ocid: str) -> Reading:
    found = _get('boot volume', ocid, lambda: session.blockstorage.get_boot_volume(ocid).data)
    if found is None:
        return Reading('boot-volume', ocid, '-', 'absent')
    return Reading('boot-volume', ocid, found.display_name, found.lifecycle_state)


def terminate(session: Session, ocid: str) -> list[Reading]:
    """Terminate the instance `ocid` names with its boot volume, and read both once they are gone.

    An instance already `TERMINATED` is read and left; its boot volume is no
    longer attached to it, so no boot volume is read beside it. Otherwise the
    boot volume is found before the termination, while it is still attached,
    the termination is waited on through its work request, and the instance
    and the boot volume are each waited on until they read `TERMINATED`.
    """
    if _kind(ocid) != 'instance':
        raise OciRefused(f'{ocid} names no instance; `terminate` takes an instance')
    log.info('reading instance %s', ocid)
    instance = _call(f'reading instance {ocid}', lambda: session.compute.get_instance(ocid).data)
    name = instance.display_name
    if instance.lifecycle_state == TERMINATED:
        log.info('%s is already %s; nothing to terminate', name, TERMINATED)
        return [Reading('instance', ocid, name, TERMINATED)]
    log.info('finding the boot volume of %s', name)
    attached = _call(
        f'listing the boot volume attachments of {name}',
        lambda: (
            oci.pagination.list_call_get_all_results(
                session.compute.list_boot_volume_attachments,
                instance.availability_domain,
                instance.compartment_id,
                instance_id=ocid,
            ).data
        ),
    )
    boot_volumes = [attachment.boot_volume_id for attachment in attached if attachment.lifecycle_state != DETACHED]
    what = f'terminating {name}'
    log.info(
        'terminating %s (%s, %s) with its boot volume %s, then waiting for its work request to end, '
        'which usually takes a few minutes (at most %ds)',
        name,
        ocid,
        instance.lifecycle_state,
        ', '.join(boot_volumes) or '(none attached)',
        TERMINATE_SECONDS,
    )
    composite = oci.core.ComputeClientCompositeOperations(session.compute, work_request_client=session.work_requests)
    result = _call(
        what,
        lambda: composite.terminate_instance_and_wait_for_work_request(
            ocid,
            operation_kwargs={'preserve_boot_volume': False},
            waiter_kwargs={
                'max_interval_seconds': POLL_SECONDS,
                'max_wait_seconds': TERMINATE_SECONDS,
                'wait_callback': _progress(f'{what}: work request'),
            },
        ),
    )
    _work_request(session, what, result)
    gone = _settle(
        name,
        session.compute,
        _call(f'reading {name}', lambda: session.compute.get_instance(ocid)),
        TERMINATED,
        seconds=SETTLE_SECONDS,
    )
    instance_reading = Reading('instance', ocid, name, 'absent' if gone is None else gone.data.lifecycle_state)
    return [instance_reading, *(_terminated_boot_volume(session, boot_volume) for boot_volume in boot_volumes)]


def _terminated_boot_volume(session: Session, ocid: str) -> Reading:
    """The boot volume a termination deleted, read once it is `TERMINATED` or gone."""
    response = _get('boot volume', ocid, lambda: session.blockstorage.get_boot_volume(ocid))
    settled = (
        None
        if response is None
        else _settle(f'boot volume {ocid}', session.blockstorage, response, TERMINATED, seconds=SETTLE_SECONDS)
    )
    if settled is None:
        return Reading('boot-volume', ocid, '-', 'absent')
    return Reading('boot-volume', ocid, settled.data.display_name, settled.data.lifecycle_state)


def detach(session: Session, ocid: str) -> list[Reading]:
    """Detach the block volume `ocid` names from its instance, and read it once the attachment is `DETACHED`.

    A volume on no instance is read and left. A volume attached to more than
    one is refused: which attachment a runbook means is the runbook's to say.
    """
    if _kind(ocid) != 'volume':
        raise OciRefused(f'{ocid} names no block volume; `detach` takes a volume')
    log.info('reading volume %s', ocid)
    volume = _call(f'reading volume {ocid}', lambda: session.blockstorage.get_volume(ocid).data)
    attachments = _attachments(session, volume)
    if not attachments:
        log.info('%s is attached to no instance; nothing to detach', volume.display_name)
        return [Reading('volume', ocid, volume.display_name, volume.lifecycle_state, ('attached-to=-',))]
    if len(attachments) > 1:
        on = ', '.join(f'{attachment.id} on {attachment.instance_id}' for attachment in attachments)
        raise OciRefused(
            f'{volume.display_name} is attached to more than one instance ({on}); detach one in the console'
        )
    (attachment,) = attachments
    what = f'detaching {volume.display_name}'
    log.info(
        'detaching %s (%s) from instance %s, attachment %s, then waiting for its work request to end, '
        'which usually takes under a minute (at most %ds)',
        volume.display_name,
        ocid,
        attachment.instance_id,
        attachment.id,
        SETTLE_SECONDS,
    )
    composite = oci.core.ComputeClientCompositeOperations(session.compute, work_request_client=session.work_requests)
    result = _call(
        what,
        lambda: composite.detach_volume_and_wait_for_work_request(
            attachment.id,
            waiter_kwargs={
                'max_interval_seconds': POLL_SECONDS,
                'max_wait_seconds': SETTLE_SECONDS,
                'wait_callback': _progress(f'{what}: work request'),
            },
        ),
    )
    _work_request(session, what, result)
    response = _call(
        f'reading attachment {attachment.id}', lambda: session.compute.get_volume_attachment(attachment.id)
    )
    _ = _settle(f'attachment {attachment.id}', session.compute, response, DETACHED, seconds=SETTLE_SECONDS)
    return [_read_volume(session, ocid)]
