"""Which resources of `physical` a rebuild changes, read off the component types that hold them.

`SCOPE` matches every resource the rebuild changes and nothing else
(operations.md §3.5): the cloud nodes with what hangs off them, the node
volumes' attachments, the day-1 chain, the worker's domain and volumes with
its image, and the stack's outputs. It names the components `CloudNodes`,
`NodeVolume`, `TalosDay1` and `HomelabHost` themselves as well, since a
component's registered outputs change with what it holds, and anchors on `::`
before each type, since each sits at the top of the stack.

The targets of steps 1, 2, 4 and 5 are patterns over the same types, each
with the count its step holds the list to before it acts (`Targets`). A
script imports no component (style/pulumi.md, "Layering"), so the types are
spelled here, and `tests/test_drill.py` holds each pattern to the URNs the
`physical` program registers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, cast

from kluster import conventions

CLOUD_NODES = 'kluster:cloud:CloudNodes'
NODE_VOLUME = 'kluster:cloud:NodeVolume'
TALOS_DAY1 = 'kluster:talos:TalosDay1'
HOMELAB_HOST = 'kluster:homelab:HomelabHost'
#: The worker's Talos image, a dynamic resource, by the name its type ends in.
FACTORY_IMAGE = 'FactoryImage'
STACK = 'pulumi:pulumi:Stack'

#: The worker's image and disk, by their logical names.
WORKER_IMAGE = f'{conventions.CLUSTER_NAME}-{conventions.HOMELAB_NODE}-nocloud'
WORKER_DISK = f'{conventions.CLUSTER_NAME}-{conventions.HOMELAB_NODE}-disk'


def _type(token: str) -> str:
    return re.escape(token)


SCOPE = re.compile(
    '|'.join(
        (
            rf'::{_type(CLOUD_NODES)}(\$|::)',
            rf'::{_type(NODE_VOLUME)}(::|\$oci:Core/volumeAttachment:)',
            rf'::{_type(TALOS_DAY1)}(\$|::)',
            rf'::{_type(HOMELAB_HOST)}(::|\$libvirt:index/(domain|volume):)',
            rf'{FACTORY_IMAGE}::{re.escape(WORKER_IMAGE)}$',
            rf'{_type(STACK)}::',
        )
    )
)


@dataclass(frozen=True)
class Targets:
    """A step's targets: the pattern over `physical`'s URNs, and how many it must find."""

    pattern: re.Pattern[str]
    count: int

    def of(self, urns: Sequence[str]) -> list[str]:
        return [urn for urn in urns if self.pattern.search(urn)]


#: Step 1: the day-1 chain, one `ConfigurationApply` per node, the
#: `Bootstrap` and the `Kubeconfig`.
DAY1 = Targets(re.compile(rf'::{_type(TALOS_DAY1)}\$talos:'), len(conventions.ALL_NODES) + 2)

#: Step 2: the worker's disk.
DISK = Targets(re.compile(rf'::{re.escape(WORKER_DISK)}$'), 1)

#: Step 4: the backend sets, whose count `LOSS` scales with.
BACKEND_SETS = re.compile(r'\$oci:NetworkLoadBalancer/backendSet:BackendSet::')


def loss(backend_sets: int) -> Targets:
    """Step 4: the instances, the dedicated VIP's two addresses, the attachments, and each node's backends.

    One backend per cloud node in each of the `backend_sets`.
    """
    nodes = len(conventions.CLOUD_NODES)
    return Targets(
        re.compile(
            rf'::{_type(CLOUD_NODES)}\$oci:(Core/instance:Instance|Core/privateIp:PrivateIp|Core/publicIp:PublicIp'
            rf'|NetworkLoadBalancer/backend:Backend)::|::{_type(NODE_VOLUME)}\$oci:Core/volumeAttachment:VolumeAttachment::'
        ),
        nodes + 2 + len(conventions.NODE_VOLUMES) + nodes * backend_sets,
    )


#: Step 4's readings: what leaves state once the refresh has read the loss.
LEFT_STATE = re.compile(
    rf'::{_type(CLOUD_NODES)}\$oci:(Core/instance:Instance|Core/privateIp:PrivateIp)::'
    rf'|::{_type(NODE_VOLUME)}\$oci:Core/volumeAttachment:VolumeAttachment::'
)
#: ... and what stays: the reserved IP and the backends.
STAYED = re.compile(rf'::{_type(CLOUD_NODES)}\$oci:(Core/publicIp:PublicIp|NetworkLoadBalancer/backend:Backend)::')

#: Step 5: the worker's image and disk, replaced.
WORKER = Targets(re.compile(rf'::{re.escape(WORKER_IMAGE)}$|::{re.escape(WORKER_DISK)}$'), 2)


def urns(show_urns: str) -> list[str]:
    """Every URN `pulumi stack --show-urns` printed."""
    return [match[1] for line in show_urns.splitlines() if (match := re.search(r'URN: (urn:\S+)$', line))]


def resources(export: str) -> list[dict[str, Any]] | None:
    """Every resource a `pulumi stack export` lists; None where what it printed is not an export."""
    try:
        document = json.loads(export)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    deployment = cast('dict[str, Any]', cast('dict[str, Any]', document).get('deployment') or {})
    return cast('list[dict[str, Any]]', deployment.get('resources') or [])


def displayed(resource: dict[str, Any]) -> object:
    """The display name a resource's state records, or None."""
    return cast('dict[str, Any]', resource.get('outputs') or {}).get('displayName')


def ocid(listed: Sequence[dict[str, Any]], kind: str, display_name: str) -> list[str]:
    """The identifier `physical`'s state records for each resource of `kind` and `display_name`."""
    return [
        str(resource.get('id'))
        for resource in listed
        if resource.get('type') == kind and displayed(resource) == display_name
    ]


def domains(listed: Sequence[dict[str, Any]], kind: str, display_names: Collection[str]) -> frozenset[str]:
    """The availability domain of each resource of `kind` among `display_names`, as its state records it."""
    return frozenset(
        str(found)
        for resource in listed
        if resource.get('type') == kind
        and displayed(resource) in display_names
        and (found := cast('dict[str, Any]', resource.get('outputs') or {}).get('availabilityDomain'))
    )


def outside_scope(preview: str) -> list[str] | None:
    """The URN of every step a `pulumi preview --json` plans outside `SCOPE`; None where it printed no plan."""
    try:
        document = json.loads(preview)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    steps = cast('list[dict[str, Any]]', cast('dict[str, Any]', document).get('steps') or [])
    return [
        str(step.get('urn')) for step in steps if step.get('op') != 'same' and not SCOPE.search(str(step.get('urn')))
    ]
