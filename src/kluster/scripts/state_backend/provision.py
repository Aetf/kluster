# The OCI SDK ships no type information (no py.typed, no stubs), so every
# value that crosses its boundary is Unknown to a strict checker. Suppressing
# that here — at the one module that touches the SDK — keeps the rest of the
# codebase under the full standard rather than relaxing it globally.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportUnknownParameterType=false
# pyright: reportMissingTypeStubs=false, reportUnknownLambdaType=false
"""Creating the appliance in OCI.

Every step that creates is an `ensure_*`: re-running converges rather than
duplicating, so "re-provision" (the box's only apply path) and "provision" are
the same command. `survey` and `found_reserved_ip` create nothing; each
`ensure_*` may, and which run calls which is `cli._provision`'s decision. The
one thing this module deliberately does not do is mutate a running box — a
changed Butane file means a new instance.

Ordering is dictated by the certificate: the server certificate is issued for
the reserved public IP, so the address must exist before the Ignition that
carries the certificate, which must exist before the instance that carries the
Ignition.

The reserved public IP is the only public address the box has -- the VNIC is
launched with no ephemeral one, and the reservation is pointed at its primary
private IP -- and `settings.ADDRESS` is that address as the repository records
it. Everything that answers with the reservation's address -- today
`ensure_reserved_ip`, `reserved_address` and `found_reserved_ip` -- holds what
OCI carries against the constant and refuses naming both when they differ,
before any caller does anything with the address (`find_reserved_ip` and the
survey answer with the reservation itself, and hold nothing): a box at another address is a decision the repository has to record,
not drift for a converge to follow. The hold is a fact about the appliance's
own compartment: a run pointed at another one (`--compartment`, or a
configuration file naming one) is another site, whose address the constant
does not describe, and `OciClients.held` is what says which kind of run this
is.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, cast

import oci

from ... import conventions
from ..credentials import oci_slot, workstation
from . import config, settings

log = logging.getLogger(__name__)

IMAGE_BUCKET = f'{settings.NAME}-images'


def _name(suffix: str) -> str:
    return f'{settings.NAME}-{suffix}'


def _data(response: Any) -> Any:
    """Unwrap an SDK response at the one boundary that is untyped by nature."""
    return response.data


@dataclass
class OciClients:
    """The OCI clients, bound to one compartment."""

    compartment_id: str
    #: What `oci.config.from_file` or the slot answered. Out of the repr and out
    #: of comparison, because the SDK carries a key's `pass_phrase` into it
    #: whenever the configuration file sets one.
    config: dict[str, Any] = field(repr=False, compare=False)
    #: Whether `compartment_id` is the appliance's own -- the one
    #: `conventions.OCI_TENANCY.compartments` records -- and so whether the
    #: reservation in it is held to `settings.ADDRESS` (`hold_address`). A run
    #: pointed at any other compartment is another site: the constant
    #: describes nothing there, and holding it would end every such run at a
    #: refusal whose only repair overwrites the real site's record.
    held: bool

    @classmethod
    def load(cls, compartment_id: str | None = None) -> OciClients:
        """The appliance's own API key, out of the workstation slot that holds it.

        The key is a §3 credential like any other (credentials.md), minted
        from the OCI seed by `credentials derived oci-state-backend mint`; the slot
        is a file because this command runs unattended halves of a bring-up
        and cannot stop to ask (`credentials.oci_slot`).

        The slot signs with the key beside its configuration, not the one its
        `key_file` entry names, so a `.credentials/` copied to a checkout at
        another path works as it is (`oci_slot.read`). A configuration the run
        is pointed at instead is the SDK's to read, entry and all.

        `OCI_CLI_CONFIG_FILE` still wins, because pointing one run at another
        tenancy is a thing an operator does and a slot is not where that
        belongs.

        Nothing else is read. A machine with neither is refused with the
        command that mints the slot: a configuration the run was not pointed
        at is not one this command can know is the appliance's, and falling
        through to it would sign as whatever key it happens to hold.

        The compartment is not part of that answer. It is a boundary this
        program decides (`conventions.OCI_TENANCY.compartments`), so the minted slot
        carries the credential alone and the mapping says where it acts;
        `--compartment` overrides both, and a configuration file an operator
        points this run at that names a `compartment-id` of its own is
        honored ahead of the mapping, because a file naming another
        tenancy's compartment means it. Which of those the answer came from
        is not remembered; whether it *is* the mapping's compartment is
        (`held`), because that is what decides whether the site's recorded
        address applies.
        """
        config: dict[str, Any]
        slot = oci_slot.config_path()
        if location := os.environ.get('OCI_CLI_CONFIG_FILE'):
            config = oci.config.from_file(location)
        elif slot.is_file():
            location = str(slot)
            config = oci_slot.read()
        else:
            raise oci_slot.SlotUnusable(
                'the appliance has no OCI credential on this machine: run `credentials derived '
                f'oci-state-backend mint`, which mints one into {slot}'
            )
        own = conventions.OCI_TENANCY.compartments[conventions.STATE_BACKEND].ocid
        compartment = compartment_id or config.get('compartment-id') or own
        if not compartment:
            # The slot carries the credential alone (`oci_slot.read`), so a
            # `compartment-id` written into it is not read: naming it there
            # would send the operator to a repair that does nothing.
            in_file = '' if location == str(slot) else f'set compartment-id in {location}, '
            raise conventions.CompartmentMissing(
                f'no compartment: pass --compartment, {in_file}or record the '
                "appliance's compartment in `conventions.OCI_TENANCY.compartments`"
            )
        return cls(compartment_id=str(compartment), config=config, held=compartment == own)

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


#: Lifecycle states a listing still carries that are not a resource anyone can
#: adopt: what the network and compute resources call `TERMINATED`, an image
#: calls `DELETED`.
GONE = ('TERMINATED', 'TERMINATING', 'DELETED')


def _lookup(list_call: Callable[..., Any], *args: Any, kind: str, name: str, **kwargs: Any) -> Any | None:
    """The one live resource of `kind` that carries `name`, or None when none does.

    Every adopt-by-name read goes through here, so the rules of adoption are
    in one place rather than at each read:

    -   **Every page.** A compartment's listing is not a list of what exists:
        a terminated resource stays in it, and this box is cattle, so the
        instance list grows by one on every replacement while the answer
        stays one box. A read of the first page alone would eventually stop
        containing the running appliance, and everything that reads this
        answer reads a miss as "absent": no approval gate in front of a
        replacement, no dump of the box about to be destroyed, and an escrow
        that mints a fresh CA and age identity over the live one.
    -   **Terminated does not count** (`GONE`): the name is free again the
        moment the old holder starts going away.
    -   **Two live holders are refused, naming both.** "Absent" and "the
        first of two" are both answers that create a duplicate, and a
        converge that adopted one of them by name would be converging a box
        the other might be serving as. Which one to keep is a decision, so
        the run stops here rather than making it.
    """
    listed = oci.pagination.list_call_get_all_results(list_call, *args, **kwargs).data
    live = [item for item in listed if item.display_name == name and item.lifecycle_state not in GONE]
    if len(live) > 1:
        raise RuntimeError(
            f'{kind}: {len(live)} live resources carry the name {name} ({", ".join(str(item.id) for item in live)}); '
            'adopting one by name would leave the other standing, so nothing proceeds until one is gone'
        )
    return live[0] if live else None


def _await_state(fetch: Callable[[], Any], target: str, *, what: str, timeout: int = 3600) -> Any:
    """Poll `fetch` until its resource reaches `target`.

    The SDK's own waiter re-raises the transient 404s the retry strategy
    above absorbs, which on an image import of ten minutes and more means losing the wait to a blip.
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
            # An import runs for ten minutes and more; silence for that long
            # is indistinguishable from a hang.
            log.info('%s: still %s after %s', what, state, _duration(elapsed))
            announced = elapsed
        if state == target:
            return resource
        if state in ('FAILED', 'TERMINATED', 'DELETED'):
            raise RuntimeError(f'{what} ended in {state}')
        time.sleep(15)
    raise TimeoutError(f'{what} never reached {target} within {_duration(timeout)}')


@dataclass(frozen=True)
class Survey:
    """Every resource the run adopts by name, read before anything is written.

    A field is the live resource under the appliance's name for that kind, or
    None for a proven absence -- every page read, nothing terminated counted,
    no second holder (`_lookup`). Each `ensure_*` creates exactly what is None
    here and adopts the rest, without a listing of its own; the instance is
    the exception, because the terminate falsifies its answer
    (`find_instance`). The gateway, subnet and security group live inside the
    VCN, so when no VCN carries the appliance's name there is nothing of the
    appliance's to look for, and they are None whenever `vcn` is.

    `fcos` is the release the run would import, read from the stream metadata:
    the image is named after it, so the read is a precondition of the image
    lookup rather than of the pipeline that imports one.

    `instance` is out of the repr and out of comparison: the SDK prints an
    instance with its launch metadata whole, and the `user_data` there is the
    Ignition the box booted with, which carries its TLS and SSH keys and the
    dump's B2 key.
    """

    instance: Any | None = field(repr=False, compare=False)
    vcn: Any | None
    gateway: Any | None
    subnet: Any | None
    security_group: Any | None
    public_ip: Any | None
    fcos: FcosArtifact
    image: Any | None


def survey(clients: OciClients) -> Survey:
    """Read what exists under the appliance's names. Writes nothing."""
    network = clients.network
    instance = find_instance(clients)
    vcn = _lookup(network.list_vcns, clients.compartment_id, kind='VCN', name=_name('vcn'))
    gateway = subnet = security_group = None
    if vcn is not None:
        gateway = _lookup(
            network.list_internet_gateways,
            clients.compartment_id,
            vcn_id=vcn.id,
            kind='internet gateway',
            name=_name('igw'),
        )
        subnet = _lookup(
            network.list_subnets, clients.compartment_id, vcn_id=vcn.id, kind='subnet', name=_name('subnet')
        )
        security_group = _lookup(
            network.list_network_security_groups,
            compartment_id=clients.compartment_id,
            vcn_id=vcn.id,
            kind='security group',
            name=_name('nsg'),
        )
    public_ip = find_reserved_ip(clients)
    fcos = fcos_artifact()
    image_name = _name(f'fcos-{fcos.release}')
    log.info('looking for an imported image named %s', image_name)
    image = _lookup(
        clients.compute.list_images, clients.compartment_id, display_name=image_name, kind='image', name=image_name
    )
    return Survey(
        instance=instance,
        vcn=vcn,
        gateway=gateway,
        subnet=subnet,
        security_group=security_group,
        public_ip=public_ip,
        fcos=fcos,
        image=image,
    )


@dataclass(frozen=True)
class Placement:
    """The VCN and the subnet everything the appliance needs is created in.

    Named apart from `OciClients.network`, which is the SDK client that talks
    to the networking service: this is the pair of identifiers that client
    produced.
    """

    vcn_id: str
    subnet_id: str


def ensure_network(clients: OciClients, found: Survey) -> Placement:
    """The appliance's VCN, gateway, default route and subnet."""
    network = clients.network
    log.info('converging the VCN, internet gateway, default route and subnet')

    vcn = found.vcn
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

    gateway = found.gateway
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

    subnet = found.subnet
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


def ensure_security_group(clients: OciClients, vcn_id: str, found: Survey) -> str:
    """5432 and 22 from anywhere.

    The client certificate is the wall (state-backend.md §4): an allowlist of
    GitHub's ranges exceeds the rule quota by an order of magnitude, and a
    home-only rule would simply break CI.
    """
    network = clients.network
    log.info('converging the security group and its rules')
    group = found.security_group
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


def hold_address(address: str, *, held: bool, fresh: bool = False) -> str:
    """The address OCI carries for the appliance, held against `settings.ADDRESS`.

    Every consumer of the address is downstream of this: the certificate is
    issued for it, the client bundles and the host-key pin are keyed by it, the
    converge waits on it, `ssh` dials it, and the probe -- which runs from
    another repository with no OCI credential -- dials `settings.ADDRESS`
    instead. They agree only if the constant is what the box carries, and a
    difference is refused before the first of them runs rather than followed:
    a box at another address is a decision the repository has to record, and a
    converge that adopted whatever it found would leave the probe reporting a
    dead certificate for a box alive somewhere else.

    A refusal names both addresses. Which one is wrong is the operator's call:
    recording the found address in `settings.py` is the answer when the move
    was meant, and repointing the reservation when it was not. A reservation
    the run has just made (`fresh`) has no second repair: OCI chose the
    address, there is nothing to repoint to, and a site provisioned for the
    first time has no line to keep -- that refusal names recording alone.

    `held` is `OciClients.held`: the constant records the address in the
    appliance's own compartment and nothing else, so a run pointed at another
    compartment is not held to it -- the address is returned as found, and
    the log says so once. Holding such a run would refuse every one of them
    with a repair (record the address) that overwrites the real site's line.
    """
    if not held:
        log.info('%s is not held to settings.ADDRESS: --compartment names another site', address)
        return address
    if address == settings.ADDRESS:
        return address
    if fresh:
        raise RuntimeError(
            f'the reserved address {_name("ip")} was just reserved at {address}, but settings.ADDRESS names '
            f'{settings.ADDRESS}: a box at another address is a decision rather than drift, so nothing here '
            f'proceeds until the two agree -- record {address} as settings.ADDRESS and re-run; the '
            'reservation stands'
        )
    raise RuntimeError(
        f'the reserved address {_name("ip")} carries is {address}, but settings.ADDRESS names '
        f'{settings.ADDRESS}: a box at another address is a decision rather than drift, so nothing here '
        f'proceeds until the two agree -- record {address} in settings.py if the move is meant, '
        'or repoint the reservation if it is not'
    )


def ensure_reserved_ip(clients: OciClients, found: Survey) -> ReservedAddress:
    """The address the server certificate is issued for.

    Held against `settings.ADDRESS` (`hold_address`, on the appliance's own
    compartment) whether it was found or just reserved. A reservation this
    call makes is one OCI chose, so it is refused on the same terms but with
    one repair rather than two: on a site that has never been provisioned the
    run ends here naming the new address to record, the operator records it,
    and the next run finds the reservation and continues -- the reservation
    itself is an `ensure_*` and stands across the refusal.
    """
    public_ip = found.public_ip
    fresh = public_ip is None
    if fresh:
        public_ip = _data(
            clients.network.create_public_ip(
                oci.core.models.CreatePublicIpDetails(
                    compartment_id=clients.compartment_id,
                    lifetime='RESERVED',
                    display_name=_name('ip'),
                )
            )
        )
        log.info('reserved %s', public_ip.ip_address)
    address = hold_address(str(public_ip.ip_address), held=clients.held, fresh=fresh)
    return ReservedAddress(id=str(public_ip.id), address=address)


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


def found_reserved_ip(clients: OciClients, found: Survey) -> ReservedAddress | None:
    """The reservation the survey found, or None when nothing is reserved. Creates nothing.

    The converge's read of the reservation, for the comparison a run makes
    before it decides whether to write anything; `ensure_reserved_ip` is the
    same read on a run that launches, reserving when this answers None. Held
    against `settings.ADDRESS` the same way (`hold_address`).
    """
    if found.public_ip is None:
        return None
    address = hold_address(str(found.public_ip.ip_address), held=clients.held)
    return ReservedAddress(id=str(found.public_ip.id), address=address)


def reserved_address(clients: OciClients) -> str:
    """The appliance's address, looked up rather than created.

    `ensure_reserved_ip` reserves one when none exists, which is right during
    provisioning and wrong for everything else: a diagnosis command must not
    allocate cloud resources as a side effect of being unable to find them.
    Held against `settings.ADDRESS` the same way (`hold_address`), so `ssh`
    never pins a host key to an address the repository does not name.
    """
    public_ip = find_reserved_ip(clients)
    if public_ip is None:
        raise RuntimeError(f'no reserved address named {_name("ip")}; has the appliance been provisioned?')
    return hold_address(str(public_ip.ip_address), held=clients.held)


def find_reserved_ip(clients: OciClients) -> Any | None:
    """The reservation under the appliance's name, if there is one. Creates nothing."""
    log.info('looking up the reserved address %s', _name('ip'))
    return _lookup(
        clients.network.list_public_ips,
        scope='REGION',
        compartment_id=clients.compartment_id,
        lifetime='RESERVED',
        kind='reserved address',
        name=_name('ip'),
    )


def pin_options(known_hosts: Path) -> list[str]:
    """The `ssh` options that hold a connection to one host key and to nothing else.

    A named list rather than an argument vector built inline, so that a test
    can drive a real OpenSSH client with the options the tool actually execs
    with: a second copy written in a test would only ever prove that the copy
    works.

    Each one is load-bearing:

    -   `-F /dev/null` -- **no client configuration participates in this
        connection at all.** The cut is stated that way rather than as a list
        of directives to fear, because suppressing the two known-hosts files
        alone leaves the rest of `ssh_config` in force and two of its
        directives defeat the pin outright. `ControlMaster`/`ControlPath`:
        one bare `ssh core@<address>` outside the tool -- the mistake the
        runbook already anticipates -- leaves a multiplexing master, and a
        later pinned exec that sees the same configuration attaches to that
        socket and runs *with no host-key verification performed at all*.
        `KnownHostsCommand`: a command the configuration names supplies
        trusted keys, and its answer is accepted alongside the pinned file.
        `-F` suppresses the system-wide configuration as well as the user's,
        which is what makes this a cut rather than an enumeration: every
        directive not on this command line is back at its default. That is
        the cost too: conveniences an operator keeps in `ssh_config` --
        `IdentityFile`, `ProxyJump`, `ServerAliveInterval` -- do not apply
        here either, so anything this connection needs is named on the
        command line or it does not happen.
    -   `UserKnownHostsFile` and `GlobalKnownHostsFile` -- the pin is the only
        answer this connection accepts, so neither the operator's file nor the
        machine's can supply a competing entry, and the tool writes neither.
    -   `StrictHostKeyChecking=yes` -- no trust-on-first-use and no prompt: an
        unknown or wrong key ends the connection instead of being written down.
    -   `HostKeyAlgorithms` -- the box also holds the host key types
        `sshd-keygen` generated for itself, and naming the pinned algorithm is
        what stops a wrong answer from steering the client onto one of those.

    `CheckHostIP` adds nothing: the connection dials the reserved address
    literally, and the entry is keyed by it.
    """
    return [
        '-F',
        '/dev/null',
        '-o',
        f'UserKnownHostsFile={known_hosts}',
        '-o',
        'GlobalKnownHostsFile=/dev/null',
        '-o',
        'StrictHostKeyChecking=yes',
        '-o',
        'HostKeyAlgorithms=ssh-ed25519',
    ]


def ssh(clients: OciClients, command: Sequence[str]) -> NoReturn:
    """Log in to the appliance, or run one command on it.

    SSH is a diagnosis path only (state-backend.md §1): the box is never
    configured by hand, and the only apply path is re-provision. This exists
    so that reading a log does not start with looking up an address.

    **The host key is pinned, and first contact is not trust-on-first-use.**
    The pin comes from the running instance's metadata over the authenticated
    control plane -- the same channel the converge reads the bill of
    materials from -- and goes into a `known_hosts` file of the tool's own
    that the client is pointed at exclusively, with no client configuration
    of any kind in force (`pin_options`). It is re-read on every exec rather
    than trusted from disk, which is what makes the check right on a
    workstation that did not perform the last replace.

    Replaces this process rather than wrapping it, so an interactive session
    gets a real terminal and the exit status is ssh's own.
    """
    address = reserved_address(clients)
    pin = host_key_pin(clients)
    known_hosts = config.write_known_hosts(workstation.bundle_dir(), address=address, public_key=pin)
    argv = ['ssh', *pin_options(known_hosts), f'core@{address}', *command]
    # `os.execvp` replaces this process, so what a failure means has to be
    # said before the connection rather than after it.
    log.info(
        'pinning %s to the host key OCI records for the running instance (%s); a refusal means the box '
        'answering is not the one that metadata describes -- either it was replaced since this read, '
        'or something is interposed on the path',
        address,
        pin,
    )
    log.info('%s', ' '.join(argv))
    os.execvp('ssh', argv)


def ensure_image(clients: OciClients, found: Survey) -> str:
    """Import the FCOS qcow2 as a custom image, once per release."""
    artifact = found.fcos
    image_name = _name(f'fcos-{artifact.release}')

    image = found.image
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


def shape_availability_domain(clients: OciClients, image_id: str) -> str:
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
    """The appliance, if it exists. Creates nothing.

    Named apart from the survey, as `find_reserved_ip` is, because it is
    read outside a converge -- `ssh` asks for the pin alone -- and read again
    inside one: `ensure_instance` asks after the terminate, when the survey's
    answer is the box that was destroyed.
    """
    return _lookup(clients.compute.list_instances, clients.compartment_id, kind='instance', name=_name('vm'))


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
#: The public half of the SSH host key the Ignition this box booted with
#: delivered. Beside the digest map for the same reason the expiry is: it is
#: not compared against the repository but read for its value, here by the one
#: command that connects to the box over SSH (`ssh`). A launch metadata entry
#: rather than a constant in `conventions`, because this box is cattle and the
#: key is a fact about the instance rather than about the repository.
HOST_KEY_METADATA = 'kluster_ssh_host_key'


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


def host_key_pin(clients: OciClients) -> str:
    """The SSH host key the running box was launched holding.

    Read over the signed control plane rather than from the box, which is the
    whole of its value: it is what the box is about to be checked against, so
    a copy the box itself supplied would check nothing. Whoever can rewrite it
    to match a rogue machine is an OCI principal with instance-update on the
    compartment, which is root-equivalent for this box already (§1).

    A box that records none is refused rather than trusted. Silence is the
    state the pin exists to rule out, and a box launched before this was
    recorded is one replace to fix.
    """
    instance = find_instance(clients)
    if instance is None:
        raise RuntimeError(f'no running {_name("vm")}; has the appliance been provisioned?')
    metadata: dict[str, Any] = dict(getattr(instance, 'metadata', None) or {})
    pin = str(metadata.get(HOST_KEY_METADATA) or '')
    if not pin:
        raise RuntimeError(
            f'{_name("vm")} records no SSH host key, so there is nothing to hold the box to: a box built '
            'before the pin was recorded is replaced with `state-backend provision --replace`'
        )
    return pin


def ensure_instance(
    clients: OciClients,
    *,
    subnet_id: str,
    nsg_id: str,
    image_id: str,
    availability_domain: str,
    ignition: str,
    digests: dict[str, str],
    dump_key_id: str,
    server_cert_expiry: str,
    ssh_host_key_pub: str,
) -> str:
    """Launch the box, or return the one already running.

    `ssh_host_key_pub` must be the public half of the key the `ignition`
    beside it carries, which is to say both must come from one `config.machine`
    call: a pin taken from a second render names a key this box was never
    given, and every later `state-backend ssh` refuses the box it describes.

    `availability_domain` must offer `settings.SHAPE` for `image_id`
    (`shape_availability_domain`); one that does not fails the launch as
    `404 NotAuthorizedOrNotFound`.
    """
    compute = clients.compute
    instance = find_instance(clients)
    if instance is not None:
        return str(instance.id)

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
                    HOST_KEY_METADATA: ssh_host_key_pub,
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
    """Point the reserved address at the instance's primary private IP.

    A box OCI is still provisioning has no attached VNIC to point at yet. A
    re-run meets one when the run before it lost the wait on a launch OCI had
    accepted, and it is refused with that said: the next run finds the box
    running, and waiting for it here would be a second wait beside the one
    the launch already has.
    """
    network = clients.network
    log.info('checking that the reserved address points at the instance')
    attachments = _data(clients.compute.list_vnic_attachments(clients.compartment_id, instance_id=instance_id))
    attached = [attachment for attachment in attachments if attachment.lifecycle_state == 'ATTACHED']
    if not attached:
        raise RuntimeError(
            f'{instance_id} has no attached network interface for the reserved address to point at: '
            'the box is still provisioning. Re-run `state-backend provision` from this commit once OCI '
            'reports it RUNNING'
        )
    vnic_id = attached[0].vnic_id
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
    """Check the pinned artifacts are what settings.py claims they are.

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
