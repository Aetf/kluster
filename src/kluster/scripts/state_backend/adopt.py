# The OCI SDK ships no type information (no py.typed, no stubs), so every
# value that crosses its boundary is Unknown to a strict checker. Suppressing
# that here, at the module that touches the SDK, keeps the rest of the package
# under the full standard.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""`state-backend adopt`: the ids of what already exists, written once into the `state-backend` stack's configuration.

The cutover's first step (rfc-006 §14, slice 6). Every resource the stack
keeps but the box exists already, built by the appliance's earlier script under
the display names the stack declares, so the stack imports each through the
program rather than creating a second one (`kluster.lib.state_backend.adoption`).
This reads each one's id and writes them all under one key, in the clear: ids
authorize nothing (rfc-006 §3.3).

**What is read, and how.** Each OCI resource by its display name in the
appliance's compartment, every page of each listing, a terminated holder of
the name not counting and two live ones refused, naming both; the image
bucket as the `n/<namespace>/b/<name>` id its importer takes;
and the dump bucket from B2 by its name. The credentials are the stack's own,
read from its configuration under the operator passphrase: the OCI key and
the B2 management key the program builds its providers from.

**What is not read.** The instance, the image and the dump key the script
made, which the stack replaces rather than imports (`adoption`), and the
security list and route table, which the program creates as its own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import oci

from kluster import conventions
from kluster.lib import stack_environment
from kluster.lib.state_backend import adoption, settings

from ..credentials import b2, derived, pulumi_config

log = logging.getLogger(__name__)

#: Lifecycle states a listing still carries that are not a resource anyone can
#: adopt: what the network and compute resources call `TERMINATED`, an image
#: calls `DELETED`.
GONE = ('TERMINATED', 'TERMINATING', 'DELETED')


class Refused(RuntimeError):
    """What exists is not what the stack can adopt, or the stack cannot be written."""


def _name(suffix: str) -> str:
    return f'{settings.NAME}-{suffix}'


def _listed(list_call: Callable[..., Any], *args: Any, **kwargs: Any) -> list[Any]:
    """Every page of a listing, as one list: a compartment's listing keeps every box ever terminated."""
    return list(oci.pagination.list_call_get_all_results(list_call, *args, **kwargs).data)


def _pick(listed: Sequence[Any], *, kind: str, name: str) -> Any:
    """The one live resource of `kind` that carries `name`, refused when there is none, or more than one."""
    live = [item for item in listed if item.display_name == name and item.lifecycle_state not in GONE]
    if not live:
        raise Refused(f'no live {kind} carries the name {name}, so there is nothing to adopt for it')
    if len(live) > 1:
        raise Refused(
            f'{kind}: {len(live)} live resources carry the name {name} ({", ".join(str(item.id) for item in live)}); '
            'which one the stack keeps is a decision, so nothing is written until one is gone'
        )
    return live[0]


@dataclass(frozen=True)
class Clients:
    """The OCI clients `adopt` reads through, and how it lists B2's buckets. Substituted in tests.

    The clients are out of the repr and out of comparison: each is built from
    a configuration carrying the stack's OCI key.
    """

    network: Any = field(repr=False, compare=False)
    object_storage: Any = field(repr=False, compare=False)
    #: The dump buckets called by a name.
    buckets: Callable[[str], Sequence[b2.Bucket]]


def find(clients: Clients, compartment_id: str) -> dict[str, str]:
    """The id of every resource the stack adopts, keyed by its name in `adoption.ADOPTABLE`. Writes nothing."""
    network = clients.network
    log.info('looking up the network under the names %s, %s and %s', _name('vcn'), _name('igw'), _name('subnet'))
    vcn = _pick(_listed(network.list_vcns, compartment_id), kind='VCN', name=_name('vcn'))
    gateway = _pick(
        _listed(network.list_internet_gateways, compartment_id, vcn_id=vcn.id),
        kind='internet gateway',
        name=_name('igw'),
    )
    subnet = _pick(_listed(network.list_subnets, compartment_id, vcn_id=vcn.id), kind='subnet', name=_name('subnet'))

    log.info('looking up the reserved address %s', _name('ip'))
    address = _pick(
        _listed(network.list_public_ips, scope='REGION', compartment_id=compartment_id, lifetime='RESERVED'),
        kind='reserved address',
        name=_name('ip'),
    )
    if address.ip_address != settings.ADDRESS:
        raise Refused(
            f'the reservation {_name("ip")} carries {address.ip_address}, and the appliance is recorded at '
            f'{settings.ADDRESS} (kluster.lib.state_backend.settings.ADDRESS)'
        )

    log.info('looking up the image bucket %s', settings.IMAGE_BUCKET)
    namespace = str(clients.object_storage.get_namespace(compartment_id=compartment_id).data)
    _ = clients.object_storage.get_bucket(namespace, settings.IMAGE_BUCKET)

    log.info('looking up the dump bucket %s in B2', settings.B2_BUCKET)
    dumps = list(clients.buckets(settings.B2_BUCKET))
    if len(dumps) != 1:
        raise Refused(f'B2 answers {len(dumps)} buckets named {settings.B2_BUCKET}, and the stack adopts one')

    return {
        adoption.VCN: str(vcn.id),
        adoption.GATEWAY: str(gateway.id),
        adoption.SUBNET: str(subnet.id),
        adoption.IMAGE_BUCKET: f'n/{namespace}/b/{settings.IMAGE_BUCKET}',
        adoption.ADDRESS: str(address.id),
        adoption.DUMP_BUCKET: dumps[0].bucket_id,
    }


def stack(checkout: Path) -> pulumi_config.Stack:
    """The `state-backend` stack's configuration in `checkout`, opened with the operator passphrase.

    Refused before its first checkpoint, as every delivery into it is: the
    stack is created by `stack init` through the driver.
    """
    name = derived.STATE_BACKEND_STACK
    if stack_environment.checkpoint(checkout, name) is None:
        raise Refused(
            f'the {name} stack has no checkpoint in {checkout}: `operator-stack {name} pulumi stack init` '
            'creates it, and the checkpoint lands before anything is written into its configuration'
        )
    passphrase: list[str] = []

    def operator() -> str:
        # Found once per run, however many keys are read.
        if not passphrase:
            passphrase.append(stack_environment.operator_passphrase(checkout))
        return passphrase[0]

    return pulumi_config.Stack(
        name=name, directory=checkout, environment=pulumi_config.BackendEnvironment(operator=operator)
    )


def clients(configuration: pulumi_config.Stack) -> Clients:
    """Clients signed with the stack's own OCI key, and B2 authorized as its own management key."""
    signing = {
        'user': configuration.get(derived.OCI_USER_KEY),
        'fingerprint': configuration.get(derived.OCI_FINGERPRINT_KEY),
        'key_content': configuration.get(derived.OCI_PRIVATE_KEY_KEY),
        'tenancy': conventions.OCI_TENANCY.tenancy_ocid,
        'region': conventions.OCI_TENANCY.region,
    }
    session = b2.Session.authorize(configuration.get(derived.B2_KEY_ID_KEY), configuration.get(derived.B2_KEY_KEY))
    return Clients(
        network=oci.core.VirtualNetworkClient(signing),
        object_storage=oci.object_storage.ObjectStorageClient(signing),
        buckets=session.buckets,
    )


def adopt(configuration: pulumi_config.Stack, found: Clients, compartment_id: str) -> dict[str, str]:
    """Write the ids of what exists into the stack's configuration, and read them back. Returns them."""
    ids = find(found, compartment_id)
    for name in adoption.ADOPTABLE:
        log.info('adopting %s: %s', name, ids[name])
    configuration.set(adoption.KEY, json.dumps(ids, sort_keys=True))
    log.info(
        'the %s stack imports these through its program on its next run: commit Pulumi.%s.yaml, and '
        '`operator-stack %s plan` shows the imports',
        configuration.name,
        configuration.name,
        configuration.name,
    )
    return ids
