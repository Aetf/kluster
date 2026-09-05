# The OCI SDK ships no type information (no py.typed, no stubs), so every
# value that crosses its boundary is Unknown to a strict checker. Suppressing
# that here — at the one module that touches the SDK — keeps the rest of the
# codebase under the full standard rather than relaxing it globally.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportMissingTypeStubs=false, reportUnknownLambdaType=false
"""Creating the appliance in OCI.

Every step is an `ensure_*`: re-running converges rather than duplicating, so
"re-provision" (the box's only apply path) and "provision" are the same
command. The one thing this module deliberately does not do is mutate a
running box — a changed Butane file means a new instance.

Ordering is dictated by the certificate: the server certificate is issued for
the reserved public IP, so the address must exist before the Ignition that
carries the certificate, which must exist before the instance that carries the
Ignition.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import lzma
import os
import shutil
import subprocess as sp
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

import oci

from ... import conventions
from ..credentials import oci_slot
from . import settings

log = logging.getLogger(__name__)

IMAGE_BUCKET = f'{settings.NAME}-images'

#: Where the appliance's key lived before it became a workstation slot: the
#: XDG path the containerized `oci` CLI reads too. Still read, once and
#: loudly, so a workstation that predates the mint keeps provisioning. It
#: lives here rather than beside the writer because this is the only thing
#: that reads it, and the two have to be deleted together.
#: TODO(kluster-ops#41): delete this and its probe below once every
#: workstation has run `credentials derived oci-state-backend mint`.
LEGACY_CONFIG_FILE = Path.home() / '.config' / 'oci' / 'config'


def _name(suffix: str) -> str:
    return f'{settings.NAME}-{suffix}'


def _data(response: Any) -> Any:
    """Unwrap an SDK response at the one boundary that is untyped by nature."""
    return response.data


@dataclass
class OciClients:
    """The OCI clients, bound to one compartment."""

    compartment_id: str
    config: dict[str, Any]

    @classmethod
    def load(cls, compartment_id: str | None = None) -> OciClients:
        """The appliance's own API key, out of the workstation slot that holds it.

        The key is a §3 credential like any other (credentials.md), minted
        from the OCI seed by `credentials derived oci-state-backend mint`; the slot
        is a file because this command runs unattended halves of a bring-up
        and cannot stop to ask (`credentials.oci_slot`).

        `OCI_CLI_CONFIG_FILE` still wins, because pointing one run at another
        tenancy is a thing an operator does and a slot is not where that
        belongs.

        A machine that still has a hand-written configuration where the slot
        used to be keeps provisioning, once and loudly: what is there is a
        complete answer, and the warning names the command that replaces it.

        The compartment is not part of that answer. It is a boundary this
        program decides (`conventions.OCI_TENANCY.compartments`), so the minted slot
        carries the credential alone and the mapping says where it acts;
        `--compartment` overrides both, and a configuration file that names a
        `compartment-id` of its own — the hand-written one above, or one an
        operator points this run at — is honoured ahead of the mapping,
        because a file naming another tenancy's compartment means it.
        """
        location = os.environ.get('OCI_CLI_CONFIG_FILE')
        if not location:
            slot = oci_slot.config_path()
            if slot.is_file():
                location = str(slot)
            elif LEGACY_CONFIG_FILE.is_file():
                log.warning(
                    'using the OCI configuration in %s: the appliance has a minted key of its own now, '
                    'which `credentials derived oci-state-backend mint` writes to %s',
                    LEGACY_CONFIG_FILE,
                    slot,
                )
                location = str(LEGACY_CONFIG_FILE)
            else:
                raise ValueError(
                    'the appliance has no OCI credential on this machine: run `credentials derived '
                    f'oci-state-backend mint`, which mints one into {slot}'
                )
        config = oci.config.from_file(location)
        compartment = (
            compartment_id
            or config.get('compartment-id')
            or conventions.OCI_TENANCY.compartments[conventions.STATE_BACKEND].ocid
        )
        if not compartment:
            raise ValueError(
                f'no compartment: pass --compartment, set compartment-id in {location}, or record the '
                "appliance's compartment in `conventions.OCI_TENANCY.compartments`"
            )
        return cls(compartment_id=str(compartment), config=config)

    @property
    def _retry(self) -> Any:
        """Retry the transient 404s a young tenancy serves.

        OCI answers `NotAuthorizedOrNotFound` for a while after an IAM change
        and, sporadically, for calls that succeed on the next attempt — the
        same authorization that just worked. Bounded retries turn that into
        latency; a real permission problem still surfaces, just later.
        """
        return (
            oci.retry.RetryStrategyBuilder(
                max_attempts_check=True,
                max_attempts=6,
                total_elapsed_time_check=True,
                total_elapsed_time_seconds=300,
                retry_max_wait_between_calls_seconds=30,
                service_error_check=True,
                service_error_retry_on_any_5xx=True,
                service_error_retry_config={404: ['NotAuthorizedOrNotFound'], 429: []},
                backoff_type=oci.retry.BACKOFF_FULL_JITTER_EQUAL_ON_THROTTLE_VALUE,
            )
            .add_service_error_check()
            .get_retry_strategy()
        )

    @property
    def network(self) -> oci.core.VirtualNetworkClient:
        return oci.core.VirtualNetworkClient(self.config, retry_strategy=self._retry)

    @property
    def compute(self) -> oci.core.ComputeClient:
        return oci.core.ComputeClient(self.config, retry_strategy=self._retry)

    @property
    def identity(self) -> oci.identity.IdentityClient:
        return oci.identity.IdentityClient(self.config, retry_strategy=self._retry)

    @property
    def object_storage(self) -> oci.object_storage.ObjectStorageClient:
        return oci.object_storage.ObjectStorageClient(self.config, retry_strategy=self._retry)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ''


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f'{minutes}m{secs:02d}s' if minutes else f'{secs}s'


def _find(items: list[Any], display_name: str) -> Any | None:
    for item in items:
        if item.display_name == display_name and item.lifecycle_state not in ('TERMINATED', 'TERMINATING'):
            return item
    return None


def _await_state(fetch: Callable[[], Any], target: str, *, what: str, timeout: int = 3600) -> Any:
    """Poll `fetch` until its resource reaches `target`.

    The SDK's own waiter re-raises the transient 404s the retry strategy
    above absorbs, which on an hour-long image import means losing the wait to a blip.
    """
    started = time.monotonic()
    deadline = started + timeout
    last = ''
    announced = 0.0
    log.info('waiting for %s to reach %s, polling every 15s (up to %s)', what, target, _duration(timeout))
    while time.monotonic() < deadline:
        try:
            resource = _data(fetch())
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise
            time.sleep(15)
            continue
        state = str(resource.lifecycle_state)
        elapsed = time.monotonic() - started
        if state != last:
            log.info('%s: %s (%s)', what, state, _duration(elapsed))
            last = state
            announced = elapsed
        elif elapsed - announced >= 60:
            # An import runs for the better part of an hour; silence for that
            # long is indistinguishable from a hang.
            log.info('%s: still %s after %s', what, state, _duration(elapsed))
            announced = elapsed
        if state == target:
            return resource
        if state in ('FAILED', 'TERMINATED', 'DELETED'):
            raise RuntimeError(f'{what} ended in {state}')
        time.sleep(15)
    raise TimeoutError(f'{what} never reached {target} within {_duration(timeout)}')


@dataclass(frozen=True)
class Placement:
    """The VCN and the subnet everything the appliance needs is created in.

    Named apart from `OciClients.network`, which is the SDK client that talks
    to the networking service: this is the pair of identifiers that client
    produced.
    """

    vcn_id: str
    subnet_id: str


def ensure_network(clients: OciClients) -> Placement:
    """The appliance's VCN, gateway, default route and subnet."""
    network = clients.network
    log.info('converging the VCN, internet gateway, default route and subnet')

    vcn = _find(_data(network.list_vcns(clients.compartment_id)), _name('vcn'))
    if vcn is None:
        vcn = _data(
            network.create_vcn(
                oci.core.models.CreateVcnDetails(
                    compartment_id=clients.compartment_id,
                    cidr_block=settings.VCN_CIDR,
                    display_name=_name('vcn'),
                    dns_label='statebackend',
                )
            )
        )
        log.info('created VCN %s', vcn.id)

    gateway = _find(_data(network.list_internet_gateways(clients.compartment_id, vcn_id=vcn.id)), _name('igw'))
    if gateway is None:
        gateway = _data(
            network.create_internet_gateway(
                oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=clients.compartment_id,
                    vcn_id=vcn.id,
                    is_enabled=True,
                    display_name=_name('igw'),
                )
            )
        )
        log.info('created internet gateway %s', gateway.id)

    route_table = _data(network.get_route_table(vcn.default_route_table_id))
    if not route_table.route_rules:
        _ = network.update_route_table(
            vcn.default_route_table_id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=[
                    oci.core.models.RouteRule(
                        destination='0.0.0.0/0',
                        destination_type='CIDR_BLOCK',
                        network_entity_id=gateway.id,
                    )
                ]
            ),
        )
        log.info('default route now points at the gateway')

    subnet = _find(_data(network.list_subnets(clients.compartment_id, vcn_id=vcn.id)), _name('subnet'))
    if subnet is None:
        subnet = _data(
            network.create_subnet(
                oci.core.models.CreateSubnetDetails(
                    compartment_id=clients.compartment_id,
                    vcn_id=vcn.id,
                    cidr_block=settings.SUBNET_CIDR,
                    display_name=_name('subnet'),
                    dns_label='sb',
                    prohibit_public_ip_on_vnic=False,
                )
            )
        )
        log.info('created subnet %s', subnet.id)

    return Placement(vcn_id=str(vcn.id), subnet_id=str(subnet.id))


def ensure_security_group(clients: OciClients, vcn_id: str) -> str:
    """5432 and 22 from anywhere.

    The client certificate is the wall (state-backend.md §4): an allowlist of
    GitHub's ranges exceeds the rule quota by an order of magnitude, and a
    home-only rule would simply break CI.
    """
    network = clients.network
    log.info('converging the security group and its rules')
    group = _find(
        _data(network.list_network_security_groups(compartment_id=clients.compartment_id, vcn_id=vcn_id)), _name('nsg')
    )
    if group is None:
        group = _data(
            network.create_network_security_group(
                oci.core.models.CreateNetworkSecurityGroupDetails(
                    compartment_id=clients.compartment_id,
                    vcn_id=vcn_id,
                    display_name=_name('nsg'),
                )
            )
        )
        log.info('created security group %s', group.id)

    wanted = {(settings.PORT, settings.PORT), (22, 22)}
    present = set()
    for rule in _data(network.list_network_security_group_security_rules(group.id)):
        if rule.direction == 'INGRESS' and rule.tcp_options is not None:
            options = rule.tcp_options.destination_port_range
            if options is not None:
                present.add((options.min, options.max))

    missing = wanted - present
    if missing:
        _ = network.add_network_security_group_security_rules(
            group.id,
            oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
                security_rules=[
                    oci.core.models.AddSecurityRuleDetails(
                        direction='INGRESS',
                        protocol='6',
                        source='0.0.0.0/0',
                        source_type='CIDR_BLOCK',
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(min=low, max=high)
                        ),
                    )
                    for low, high in sorted(missing)
                ]
            ),
        )
        log.info('opened %s', sorted(missing))

    egress = [r for r in _data(network.list_network_security_group_security_rules(group.id)) if r.direction == 'EGRESS']
    if not egress:
        _ = network.add_network_security_group_security_rules(
            group.id,
            oci.core.models.AddNetworkSecurityGroupSecurityRulesDetails(
                security_rules=[
                    oci.core.models.AddSecurityRuleDetails(
                        direction='EGRESS',
                        protocol='all',
                        destination='0.0.0.0/0',
                        destination_type='CIDR_BLOCK',
                    )
                ]
            ),
        )
        log.info('allowed egress')

    return str(group.id)


@dataclass(frozen=True)
class ReservedAddress:
    """A reserved public IP: what it is called in the API, and what it is.

    Both halves travel together because the two are indistinguishable strings
    with different uses -- one addresses the reservation, the other is issued
    into a certificate -- and a caller free to pass them apart is a caller
    free to swap them.
    """

    id: str
    address: str


def ensure_reserved_ip(clients: OciClients) -> ReservedAddress:
    """The address the server certificate is issued for."""
    network = clients.network
    log.info('looking up the reserved address %s', _name('ip'))
    existing = _data(
        network.list_public_ips(scope='REGION', compartment_id=clients.compartment_id, lifetime='RESERVED')
    )
    public_ip = _find(existing, _name('ip'))
    if public_ip is None:
        public_ip = _data(
            network.create_public_ip(
                oci.core.models.CreatePublicIpDetails(
                    compartment_id=clients.compartment_id,
                    lifetime='RESERVED',
                    display_name=_name('ip'),
                )
            )
        )
        log.info('reserved %s', public_ip.ip_address)
    return ReservedAddress(id=str(public_ip.id), address=str(public_ip.ip_address))


@dataclass(frozen=True)
class FcosArtifact:
    """The compressed disk image one FCOS release publishes for this platform."""

    release: str
    url: str
    sha256: str


#: Where the stream metadata keeps this platform's image, key by key. Named
#: because the refusal below quotes it: a stream that stops publishing for
#: Oracle Cloud has to say which step of the descent failed.
_ARTIFACT_PATH = ('architectures', 'x86_64', 'artifacts', 'oraclecloud')


def fcos_artifact() -> FcosArtifact:
    """The pinned stream's qcow2, out of the stream metadata Fedora publishes."""
    log.info('fetching the FCOS %s stream metadata from %s', settings.FCOS_STREAM, settings.FCOS_STREAM_URL)
    with urllib.request.urlopen(settings.FCOS_STREAM_URL, timeout=60) as response:
        stream: object = json.load(response)
    what = f'the FCOS {settings.FCOS_STREAM} stream metadata'
    platform = _descend(stream, _ARTIFACT_PATH, what=what)
    disk = _descend(platform, ('formats', 'qcow2.xz', 'disk'), what=what)
    return FcosArtifact(
        release=_string(platform, 'release', what=what),
        url=_string(disk, 'location', what=what),
        sha256=_string(disk, 'sha256', what=what),
    )


def _descend(document: object, path: Sequence[str], *, what: str) -> object:
    """`document` at `path`, or a refusal naming the key that was not there.

    The boundary for a document this program did not write: what it holds is
    another project's decision, so a key it stops publishing is reported as
    the missing key rather than as a `KeyError` several levels into one
    expression.
    """
    found = document
    for step in path:
        if not isinstance(found, dict) or step not in found:
            raise RuntimeError(f'{what} has no {".".join(path)}: nothing under {step}')
        found = cast('dict[str, object]', found)[step]
    return found


def _string(document: object, key: str, *, what: str) -> str:
    """One non-empty string field of such a document, refused by name if absent."""
    value = cast('dict[str, object]', document).get(key) if isinstance(document, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f'{what} has no {key}, and holds {document!r}')
    return value


def reserved_address(clients: OciClients) -> str:
    """The appliance's address, looked up rather than created.

    `ensure_reserved_ip` reserves one when none exists, which is right during
    provisioning and wrong for everything else: a diagnosis command must not
    allocate cloud resources as a side effect of being unable to find them.
    """
    existing = _data(
        clients.network.list_public_ips(scope='REGION', compartment_id=clients.compartment_id, lifetime='RESERVED')
    )
    public_ip = _find(existing, _name('ip'))
    if public_ip is None:
        raise RuntimeError(f'no reserved address named {_name("ip")}; has the appliance been provisioned?')
    return str(public_ip.ip_address)


def ssh(clients: OciClients, command: Sequence[str]) -> NoReturn:
    """Log in to the appliance, or run one command on it.

    SSH is a diagnosis path only (state-backend.md §1): the box is never
    configured by hand, and the only apply path is re-provision. This exists
    so that reading a log does not start with looking up an address.

    Replaces this process rather than wrapping it, so an interactive session
    gets a real terminal and the exit status is ssh's own.
    """
    address = reserved_address(clients)
    argv = ['ssh', f'core@{address}', *command]
    log.info('%s', ' '.join(argv))
    os.execvp('ssh', argv)


def ensure_image(clients: OciClients) -> str:
    """Import the FCOS qcow2 as a custom image, once per release."""
    artifact = fcos_artifact()
    image_name = _name(f'fcos-{artifact.release}')

    log.info('looking for an imported image named %s', image_name)
    image = _find(_data(clients.compute.list_images(clients.compartment_id, display_name=image_name)), image_name)
    if image is not None:
        # An import in flight is not yet a bootable image; launching against
        # one fails, so converge on the finished state rather than its name.
        image_id = str(image.id)
        if str(image.lifecycle_state) != 'AVAILABLE':
            _ = _await_state(lambda: clients.compute.get_image(image_id), 'AVAILABLE', what=f'image {image_name}')
        return image_id

    log.info('no image for this release yet; checking the image bucket %s', IMAGE_BUCKET)
    namespace = _data(clients.object_storage.get_namespace())
    storage = clients.object_storage
    try:
        _ = storage.get_bucket(namespace, IMAGE_BUCKET)
    except oci.exceptions.ServiceError as exc:
        if exc.status != 404:
            raise
        _ = _data(
            storage.create_bucket(
                namespace,
                oci.object_storage.models.CreateBucketDetails(
                    name=IMAGE_BUCKET, compartment_id=clients.compartment_id, public_access_type='NoPublicAccess'
                ),
            )
        )
        log.info('created image bucket %s', IMAGE_BUCKET)

    object_name = f'fedora-coreos-{artifact.release}.qcow2'
    with tempfile.TemporaryDirectory() as tmp:
        compressed = Path(tmp) / 'fcos.qcow2.xz'
        log.info('downloading the FCOS qcow2 (~1 GiB compressed): %s', artifact.url)
        with urllib.request.urlopen(artifact.url, timeout=600) as response, compressed.open('wb') as out:
            shutil.copyfileobj(response, out)

        # Streamed: the image is most of a gigabyte, and this box has 1 GB.
        log.info('checking the download against the pinned sha256')
        hasher = hashlib.sha256()
        with compressed.open('rb') as check:
            for chunk in iter(lambda: check.read(1 << 20), b''):
                hasher.update(chunk)
        downloaded = hasher.hexdigest()
        if downloaded != artifact.sha256:
            raise RuntimeError(f'FCOS image digest mismatch: {downloaded} != {artifact.sha256}')
        log.info('digest matches; decompressing the qcow2 (a few minutes)')

        qcow = Path(tmp) / 'fcos.qcow2'
        with lzma.open(compressed) as src, qcow.open('wb') as out:
            shutil.copyfileobj(src, out)

        log.info(
            'uploading %s to %s (%.1f GiB, several minutes)', object_name, IMAGE_BUCKET, qcow.stat().st_size / 2**30
        )
        oci.object_storage.UploadManager(storage, allow_parallel_uploads=True).upload_file(
            namespace, IMAGE_BUCKET, object_name, str(qcow)
        )

    created = _data(
        clients.compute.create_image(
            oci.core.models.CreateImageDetails(
                compartment_id=clients.compartment_id,
                display_name=image_name,
                launch_mode='PARAVIRTUALIZED',
                image_source_details=oci.core.models.ImageSourceViaObjectStorageTupleDetails(
                    namespace_name=namespace,
                    bucket_name=IMAGE_BUCKET,
                    object_name=object_name,
                    source_image_type='QCOW2',
                ),
            )
        )
    )
    log.info('importing image %s', created.id)
    image = _await_state(lambda: clients.compute.get_image(created.id), 'AVAILABLE', what=f'image {image_name}')
    return str(image.id)


def _shape_domain(clients: OciClients, image_id: str) -> str:
    """An availability domain that actually offers the shape.

    Not `domains[0]`: a shape is offered per-AD, and this one is offered in
    exactly one of Phoenix's three. Launching into an AD that does not have it
    fails as `404 NotAuthorizedOrNotFound` -- an error that names neither the
    shape nor the domain, and reads like a permissions problem.
    """
    log.info('looking for an availability domain that offers %s', settings.SHAPE)
    domains = [str(domain.name) for domain in _data(clients.identity.list_availability_domains(clients.compartment_id))]
    for domain in domains:
        offered = oci.pagination.list_call_get_all_results(
            clients.compute.list_shapes,
            clients.compartment_id,
            availability_domain=domain,
            image_id=image_id,
        ).data
        if settings.SHAPE in {str(shape.shape) for shape in offered}:
            return domain
    raise RuntimeError(f'{settings.SHAPE} is offered in none of {", ".join(domains)} for this image')


def find_instance(clients: OciClients) -> Any | None:
    """The appliance, if it exists. Creates nothing."""
    return _find(_data(clients.compute.list_instances(clients.compartment_id)), _name('vm'))


#: What the box was built from, carried on the box. Instance metadata rather
#: than a freeform tag because a tag value stops at 256 characters and the
#: per-component digest map does not fit; it rides beside `user_data`, which
#: is the same fact in unreadable form.
CONFIG_METADATA = 'kluster_config'
DUMP_KEY_METADATA = 'kluster_dump_key_id'
#: When the server certificate in the Ignition this box booted with stops
#: being valid. Beside the digest map rather than inside it: every digested
#: component is re-derived from the repository and compared for equality,
#: while this one is compared against the clock (`config.renewal_due`).
EXPIRY_METADATA = 'kluster_server_cert_expiry'


@dataclass(frozen=True)
class InstanceConfig:
    """What a running box says it was built from.

    One record because the pieces come off the same metadata and are judged
    together: a box whose digests are current, but whose dump key B2 no longer
    has or whose certificate is weeks from expiring, is exactly as stale as one
    whose Butane file changed.
    """

    digests: dict[str, str]
    dump_key_id: str
    server_cert_expiry: str


def instance_config(instance: Any) -> InstanceConfig:
    """The digests and dump key id a running box was built with.

    Absent or unparsable metadata answers empty, which every caller reads as
    drift: a box that cannot say what it is gets rebuilt rather than assumed
    current.
    """
    metadata: dict[str, Any] = dict(getattr(instance, 'metadata', None) or {})
    raw = str(metadata.get(CONFIG_METADATA) or '')
    try:
        digests: dict[str, str] = {str(key): str(value) for key, value in json.loads(raw).items()}
    except (json.JSONDecodeError, AttributeError):
        digests = {}
    return InstanceConfig(
        digests=digests,
        dump_key_id=str(metadata.get(DUMP_KEY_METADATA) or ''),
        server_cert_expiry=str(metadata.get(EXPIRY_METADATA) or ''),
    )


def terminate_instance(clients: OciClients, instance_id: str) -> None:
    """Terminate the box and wait for it to be gone.

    The boot volume goes with it: the appliance holds nothing a `pg_dump`
    restore cannot rebuild (state-backend.md §1), and a preserved volume
    would be a second copy of the state to keep track of. That is a claim
    about a dump that exists, so taking one is the caller's precondition
    rather than this function's business — `cli._provision` takes and verifies
    it before calling here, and stops when it cannot.
    """
    log.info('terminating %s', instance_id)
    _ = clients.compute.terminate_instance(instance_id, preserve_boot_volume=False)
    # `_await_state` checks the target before its failure states, so asking
    # for TERMINATED here is not asking for the one it raises on.
    _ = _await_state(
        lambda: clients.compute.get_instance(instance_id), 'TERMINATED', what='the old instance', timeout=900
    )


def forget_host_key(address: str) -> None:
    """Drop the terminated box's host key from the operator's known_hosts.

    The address is reserved and the box is cattle, so every replace hands the
    same address a freshly generated host key -- and ssh, correctly, refuses
    the next login as a possible man-in-the-middle. The identity that changed
    is the one this command just destroyed, so forgetting it here is the
    honest bookkeeping; the alternative is an operator pasting `ssh-keygen -R`
    from an alarming error message on every re-provision.
    """
    if shutil.which('ssh-keygen') is None:  # pragma: no cover - ssh is a hard dependency of `ssh`
        return
    removal = sp.run(['ssh-keygen', '-R', address], capture_output=True, text=True, timeout=30)
    if removal.returncode == 0:
        log.info('removed the old host key for %s from known_hosts', address)


def ensure_instance(
    clients: OciClients,
    *,
    subnet_id: str,
    nsg_id: str,
    image_id: str,
    ignition: str,
    digests: dict[str, str],
    dump_key_id: str,
    server_cert_expiry: str,
) -> str:
    """Launch the box, or return the one already running."""
    compute = clients.compute
    instance = find_instance(clients)
    if instance is not None:
        return str(instance.id)

    availability_domain = _shape_domain(clients, image_id)
    log.info('launching %s (%s) in %s', _name('vm'), settings.SHAPE, availability_domain)
    launched = _data(
        compute.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                compartment_id=clients.compartment_id,
                availability_domain=availability_domain,
                display_name=_name('vm'),
                shape=settings.SHAPE,
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image_id, boot_volume_size_in_gbs=settings.BOOT_VOLUME_GB
                ),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet_id,
                    nsg_ids=[nsg_id],
                    assign_public_ip=False,
                    display_name=_name('vnic'),
                ),
                metadata={
                    'user_data': base64.b64encode(ignition.encode()).decode(),
                    # What the next converge compares against, in the one
                    # place that cannot drift from the box: the box.
                    CONFIG_METADATA: json.dumps(digests, sort_keys=True),
                    DUMP_KEY_METADATA: dump_key_id,
                    EXPIRY_METADATA: server_cert_expiry,
                },
                # Legacy IMDS serves the machine config without authentication.
                instance_options=oci.core.models.InstanceOptions(are_legacy_imds_endpoints_disabled=True),
            )
        )
    )
    log.info('launched %s', launched.id)
    _ = _await_state(lambda: compute.get_instance(launched.id), 'RUNNING', what=_name('vm'), timeout=1800)
    return str(launched.id)


def attach_reserved_ip(clients: OciClients, *, instance_id: str, public_ip_id: str) -> None:
    """Point the reserved address at the instance's primary private IP."""
    network = clients.network
    log.info('checking that the reserved address points at the instance')
    attachments = _data(clients.compute.list_vnic_attachments(clients.compartment_id, instance_id=instance_id))
    vnic_id = attachments[0].vnic_id
    private_ips = _data(network.list_private_ips(vnic_id=vnic_id))
    primary = next(ip for ip in private_ips if ip.is_primary)

    current = _data(network.get_public_ip(public_ip_id))
    if current.private_ip_id == primary.id:
        return
    _ = network.update_public_ip(public_ip_id, oci.core.models.UpdatePublicIpDetails(private_ip_id=primary.id))
    log.info('attached the reserved address to the instance')


def wait_for_backend(address: str, *, timeout: int = 900) -> bool:
    """The box is up when it answers a TLS handshake on 5432.

    First boot pulls the Postgres image and fetches age, so this is minutes,
    not seconds.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    announced = 0.0
    log.info(
        'waiting for a TLS handshake on %s:%d, probing every 15s — first boot pulls the Postgres image '
        'and fetches age, so this is minutes (up to %s)',
        address,
        settings.PORT,
        _duration(timeout),
    )
    reason = 'not tried yet'
    while time.monotonic() < deadline:
        try:
            probe = sp.run(
                ['openssl', 's_client', '-connect', f'{address}:{settings.PORT}', '-starttls', 'postgres', '-brief'],
                # s_client keeps the connection open reading stdin after the
                # handshake, so an inherited terminal makes a *successful*
                # probe hang until the timeout below and report itself as no
                # answer -- the wait could never finish once the port opened.
                stdin=sp.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
            answered = probe.returncode == 0
            reason = _first_line(probe.stderr) or f'openssl exited {probe.returncode}'
        except sp.TimeoutExpired:
            # Two very different things look like this, which is why the
            # reason is reported rather than swallowed: Postgres binds 5432
            # before initdb finishes and then says nothing, and a firewall on
            # the path drops the packets instead of refusing them. Treating
            # either as fatal ends the wait at the moment the box comes up.
            answered = False
            reason = 'no answer within 30s — either still starting, or the packets are being dropped'
        if answered:
            return True
        elapsed = time.monotonic() - started
        if elapsed - announced >= 60:
            log.info('still waiting after %s: %s', _duration(elapsed), reason)
            announced = elapsed
        time.sleep(15)
    log.error('last attempt said: %s', reason)
    log.error(
        'the appliance may be healthy and this path blocked: `state-backend ssh` reaches it over 22, '
        'and `openssl s_client -connect %s:%d -starttls postgres -brief </dev/null` from another host '
        'separates a broken box from a broken route',
        address,
        settings.PORT,
    )
    return False


#: What a registry names a manifest by, and the whole of what this reads off
#: the HEAD response.
DIGEST_HEADER = 'Docker-Content-Digest'


def _image_digest(image: str) -> str:
    """The digest `image`'s tag currently resolves to, via the registry API.

    Anonymous pull scope is enough to read a manifest, so this needs no
    credential -- which is the point: the check has to run on a PR from a
    fork's CI as readily as on main.
    """
    repository, tag = image.rsplit(':', 1)
    if repository.startswith('docker.io/'):
        repository = repository.removeprefix('docker.io/')
    log.info('asking the registry what %s resolves to', image)

    token_url = f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull'
    with urllib.request.urlopen(token_url, timeout=60) as response:
        token = _string(json.load(response), 'token', what=f'the registry pull token for {repository}')

    request = urllib.request.Request(
        f'https://registry-1.docker.io/v2/{repository}/manifests/{tag}',
        method='HEAD',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.oci.image.index.v1+json,'
            'application/vnd.docker.distribution.manifest.list.v2+json,'
            'application/vnd.oci.image.manifest.v1+json,'
            'application/vnd.docker.distribution.manifest.v2+json',
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        # `.get`, because a header this asks for and does not receive is the
        # case below rather than a `KeyError` from inside a mapping.
        digest = response.headers.get(DIGEST_HEADER, '')
    if not digest:
        # An empty answer would otherwise be logged as a resolution, and the
        # check that exists to catch a bad pin would report success.
        raise RuntimeError(f'the registry answered for {image} without a {DIGEST_HEADER} header')
    return str(digest)


def verify_pins() -> bool:
    """Check the pinned artefacts are what settings.py claims they are.

    Renovate can bump a version but cannot compute the tarball's digest or
    ask a registry whether a tag exists, so this runs in CI on every PR: a
    bump that leaves AGE_SHA256 stale, or names a Postgres tag that was
    never published, fails here rather than at first boot -- where the first
    strands the appliance without an encryptor and the second leaves it
    without a database.
    """
    ok = True

    log.info('downloading age %s to hash it against its pin: %s', settings.AGE_VERSION, settings.AGE_URL)
    hasher = hashlib.sha256()
    with urllib.request.urlopen(settings.AGE_URL, timeout=300) as response:
        for chunk in iter(lambda: response.read(1 << 20), b''):
            hasher.update(chunk)
    age_digest = hasher.hexdigest()
    if age_digest != settings.AGE_SHA256:
        log.error('age: pinned %s, actual %s', settings.AGE_SHA256, age_digest)
        ok = False
    else:
        log.info('age %s matches its pin', settings.AGE_VERSION)

    try:
        image_digest = _image_digest(settings.POSTGRES_IMAGE)
    except (urllib.error.HTTPError, RuntimeError) as exc:
        log.error('%s: registry says %s (does the tag exist?)', settings.POSTGRES_IMAGE, exc)
        ok = False
    else:
        # Logged rather than pinned: the tag is the major line on purpose
        # (podman-auto-update follows the minor stream, settings.py), so the
        # digest moving is the design working, not a drift to fail on.
        log.info('%s resolves to %s', settings.POSTGRES_IMAGE, image_digest)

    log.info(
        'FCOS %s stream is at %s (imported per release, no pin to drift)',
        settings.FCOS_STREAM,
        fcos_artifact().release,
    )
    return ok
