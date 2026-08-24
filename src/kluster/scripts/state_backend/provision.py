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
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import oci

from . import settings

log = logging.getLogger(__name__)

IMAGE_BUCKET = f'{settings.NAME}-images'


def _name(suffix: str) -> str:
    return f'{settings.NAME}-{suffix}'


def _data(response: Any) -> Any:
    """Unwrap an SDK response at the one boundary that is untyped by nature."""
    return response.data


@dataclass
class Oci:
    """The OCI clients, bound to one compartment."""

    compartment_id: str
    config: dict[str, Any]

    #: The SDK defaults to ~/.oci/config; this estate keeps it under XDG,
    #: where the containerized CLI reads it from too.
    CONFIG_FILE = Path.home() / '.config' / 'oci' / 'config'

    @classmethod
    def load(cls, compartment_id: str | None = None) -> Oci:
        location = os.environ.get('OCI_CLI_CONFIG_FILE') or str(cls.CONFIG_FILE)
        config = oci.config.from_file(location)
        compartment = compartment_id or config.get('compartment-id')
        if not compartment:
            raise ValueError('no compartment: pass --compartment or set compartment-id in ~/.oci/config')
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


def _find(items: list[Any], display_name: str) -> Any | None:
    for item in items:
        if item.display_name == display_name and item.lifecycle_state not in ('TERMINATED', 'TERMINATING'):
            return item
    return None


def _await_state(fetch: Any, target: str, *, what: str, timeout: int = 3600) -> Any:
    """Poll `fetch` until its resource reaches `target`.

    The SDK's own waiter re-raises the transient 404s described on the client
    above, which on an hour-long image import means losing the wait to a blip.
    """
    deadline = time.monotonic() + timeout
    last = ''
    while time.monotonic() < deadline:
        try:
            resource = _data(fetch())
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise
            time.sleep(15)
            continue
        state = str(resource.lifecycle_state)
        if state != last:
            log.info('%s: %s', what, state)
            last = state
        if state == target:
            return resource
        if state in ('FAILED', 'TERMINATED', 'DELETED'):
            raise RuntimeError(f'{what} ended in {state}')
        time.sleep(15)
    raise TimeoutError(f'{what} never reached {target}')


def ensure_network(client: Oci) -> tuple[str, str]:
    """The appliance's VCN, gateway, route and subnet. Returns (vcn, subnet)."""
    network = client.network

    vcn = _find(_data(network.list_vcns(client.compartment_id)), _name('vcn'))
    if vcn is None:
        vcn = _data(
            network.create_vcn(
                oci.core.models.CreateVcnDetails(
                    compartment_id=client.compartment_id,
                    cidr_block=settings.VCN_CIDR,
                    display_name=_name('vcn'),
                    dns_label='statebackend',
                )
            )
        )
        log.info('created VCN %s', vcn.id)

    gateway = _find(_data(network.list_internet_gateways(client.compartment_id, vcn_id=vcn.id)), _name('igw'))
    if gateway is None:
        gateway = _data(
            network.create_internet_gateway(
                oci.core.models.CreateInternetGatewayDetails(
                    compartment_id=client.compartment_id,
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

    subnet = _find(_data(network.list_subnets(client.compartment_id, vcn_id=vcn.id)), _name('subnet'))
    if subnet is None:
        subnet = _data(
            network.create_subnet(
                oci.core.models.CreateSubnetDetails(
                    compartment_id=client.compartment_id,
                    vcn_id=vcn.id,
                    cidr_block=settings.SUBNET_CIDR,
                    display_name=_name('subnet'),
                    dns_label='sb',
                    prohibit_public_ip_on_vnic=False,
                )
            )
        )
        log.info('created subnet %s', subnet.id)

    return str(vcn.id), str(subnet.id)


def ensure_security_group(client: Oci, vcn_id: str) -> str:
    """5432 and 22 from anywhere.

    The client certificate is the wall (state-backend.md §4): an allowlist of
    GitHub's ranges exceeds the rule quota by an order of magnitude, and a
    home-only rule would simply break CI.
    """
    network = client.network
    group = _find(
        _data(network.list_network_security_groups(compartment_id=client.compartment_id, vcn_id=vcn_id)), _name('nsg')
    )
    if group is None:
        group = _data(
            network.create_network_security_group(
                oci.core.models.CreateNetworkSecurityGroupDetails(
                    compartment_id=client.compartment_id,
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


def ensure_reserved_ip(client: Oci) -> tuple[str, str]:
    """The address the server certificate is issued for. Returns (id, address)."""
    network = client.network
    existing = _data(network.list_public_ips(scope='REGION', compartment_id=client.compartment_id, lifetime='RESERVED'))
    public_ip = _find(existing, _name('ip'))
    if public_ip is None:
        public_ip = _data(
            network.create_public_ip(
                oci.core.models.CreatePublicIpDetails(
                    compartment_id=client.compartment_id,
                    lifetime='RESERVED',
                    display_name=_name('ip'),
                )
            )
        )
        log.info('reserved %s', public_ip.ip_address)
    return str(public_ip.id), str(public_ip.ip_address)


def fcos_artifact() -> tuple[str, str, str]:
    """The pinned stream's qcow2: (release, url, sha256 of the compressed file)."""
    with urllib.request.urlopen(settings.FCOS_STREAM_URL, timeout=60) as response:
        stream: dict[str, Any] = json.load(response)
    disk = stream['architectures']['x86_64']['artifacts']['oraclecloud']['formats']['qcow2.xz']['disk']
    release = stream['architectures']['x86_64']['artifacts']['oraclecloud']['release']
    return str(release), str(disk['location']), str(disk['sha256'])


def ensure_image(client: Oci) -> str:
    """Import the FCOS qcow2 as a custom image, once per release."""
    release, url, sha256 = fcos_artifact()
    image_name = _name(f'fcos-{release}')

    image = _find(_data(client.compute.list_images(client.compartment_id, display_name=image_name)), image_name)
    if image is not None:
        return str(image.id)

    namespace = _data(client.object_storage.get_namespace())
    storage = client.object_storage
    try:
        _ = storage.get_bucket(namespace, IMAGE_BUCKET)
    except oci.exceptions.ServiceError as exc:
        if exc.status != 404:
            raise
        _ = _data(
            storage.create_bucket(
                namespace,
                oci.object_storage.models.CreateBucketDetails(
                    name=IMAGE_BUCKET, compartment_id=client.compartment_id, public_access_type='NoPublicAccess'
                ),
            )
        )
        log.info('created image bucket %s', IMAGE_BUCKET)

    object_name = f'fedora-coreos-{release}.qcow2'
    with tempfile.TemporaryDirectory() as tmp:
        compressed = Path(tmp) / 'fcos.qcow2.xz'
        log.info('downloading %s', url)
        with urllib.request.urlopen(url, timeout=600) as response, compressed.open('wb') as out:
            shutil.copyfileobj(response, out)

        # Streamed: the image is most of a gigabyte, and this box has 1 GB.
        digest = hashlib.sha256()
        with compressed.open('rb') as check:
            for chunk in iter(lambda: check.read(1 << 20), b''):
                digest.update(chunk)
        digest = digest.hexdigest()
        if digest != sha256:
            raise RuntimeError(f'FCOS image digest mismatch: {digest} != {sha256}')
        log.info('digest verified')

        qcow = Path(tmp) / 'fcos.qcow2'
        with lzma.open(compressed) as src, qcow.open('wb') as out:
            shutil.copyfileobj(src, out)

        log.info('uploading %s (%.1f GiB)', object_name, qcow.stat().st_size / 2**30)
        oci.object_storage.UploadManager(storage, allow_parallel_uploads=True).upload_file(
            namespace, IMAGE_BUCKET, object_name, str(qcow)
        )

    created = _data(
        client.compute.create_image(
            oci.core.models.CreateImageDetails(
                compartment_id=client.compartment_id,
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
    image = _await_state(lambda: client.compute.get_image(created.id), 'AVAILABLE', what=f'image {image_name}')
    return str(image.id)


def ensure_instance(client: Oci, *, subnet_id: str, nsg_id: str, image_id: str, ignition: str) -> str:
    """Launch the box, or return the one already running."""
    compute = client.compute
    instance = _find(_data(compute.list_instances(client.compartment_id)), _name('vm'))
    if instance is not None:
        return str(instance.id)

    availability_domain = _data(client.identity.list_availability_domains(client.compartment_id))[0].name
    launched = _data(
        compute.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                compartment_id=client.compartment_id,
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
                metadata={'user_data': base64.b64encode(ignition.encode()).decode()},
                # Legacy IMDS serves the machine config without authentication.
                instance_options=oci.core.models.InstanceOptions(are_legacy_imds_endpoints_disabled=True),
            )
        )
    )
    log.info('launched %s', launched.id)
    _ = _await_state(lambda: compute.get_instance(launched.id), 'RUNNING', what=_name('vm'), timeout=1800)
    return str(launched.id)


def attach_reserved_ip(client: Oci, *, instance_id: str, public_ip_id: str) -> None:
    """Point the reserved address at the instance's primary private IP."""
    network = client.network
    attachments = _data(client.compute.list_vnic_attachments(client.compartment_id, instance_id=instance_id))
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
    while time.monotonic() < deadline:
        probe = sp.run(
            ['openssl', 's_client', '-connect', f'{address}:{settings.PORT}', '-starttls', 'postgres', '-brief'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode == 0:
            return True
        time.sleep(15)
    return False


def verify_pins() -> bool:
    """Check the pinned artefacts still hash to what settings.py claims.

    Renovate can bump a version but cannot compute the tarball's digest, so
    this runs in CI on every PR: a bump that leaves AGE_SHA256 stale fails
    here rather than at first boot, where it would strand the appliance
    without an encryptor.
    """
    ok = True

    digest = hashlib.sha256()
    with urllib.request.urlopen(settings.AGE_URL, timeout=300) as response:
        for chunk in iter(lambda: response.read(1 << 20), b''):
            digest.update(chunk)
    if digest.hexdigest() != settings.AGE_SHA256:
        log.error('age: pinned %s, actual %s', settings.AGE_SHA256, digest.hexdigest())
        ok = False
    else:
        log.info('age %s matches its pin', settings.AGE_VERSION)

    release, _, _ = fcos_artifact()
    log.info('FCOS %s stream is at %s (imported per release, no pin to drift)', settings.FCOS_STREAM, release)
    return ok
