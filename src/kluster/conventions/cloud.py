"""The cloud site: the node fleet, its sizing, its network plan, its per-node capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network

from kluster.conventions.homelab import HOMELAB_NODE

#: Three combined control-plane/ingress nodes (architecture.md §1.1).
CLOUD_NODES = ('cp1', 'cp2', 'cp3')

#: Every Talos node this program declares.
ALL_NODES = (*CLOUD_NODES, HOMELAB_NODE)

#: Designed against the conservative half of the A1 allowance (2 OCPU/12 GB),
#: so the architecture stays valid if the free tier halves again (nodes.md §3.2).
NODE_OCPUS = 1
NODE_MEMORY_GB = 8
NODE_BOOT_VOLUME_GB = 50

#: The cluster VCN. Chosen clear of everything it must coexist with: the
#: state-backend appliance's own network, the pod and service ranges, the home
#: VLANs, the ZeroTier range, and the legacy cluster's 10.42/10.43.
VCN_CIDR = IPv4Network('10.20.0.0/16')
VCN_SUBNET_CIDR = IPv4Network('10.20.0.0/24')

#: The node holding the dedicated VIP: a secondary private address and the
#: reserved public address mapped onto it, which exactly one node has
#: (architecture.md §3.2). A workload that must be reached at that address, and
#: leave by it, is scheduled here.
DEDICATED_VIP_NODE = 'cp1'


@dataclass(frozen=True)
class FollowsDedicatedVip:
    """A volume's node, stated as whichever node holds the dedicated VIP."""


FOLLOWS_DEDICATED_VIP = FollowsDedicatedVip()


@dataclass(frozen=True)
class NodeVolumeEntry:
    """A block volume attached to one node, and where that node mounts it.

    `node` names a node of the fleet, or `FOLLOWS_DEDICATED_VIP` where the
    dataset belongs on whichever node carries the VIP — a workload whose
    traffic must leave by the address it arrives on, with its cache pinned to
    that machine. The sentinel is what makes "the VIP moved, the volume stayed"
    a state nobody can write; a `DEDICATED_VIP_NODE` edit re-declares the
    attachment, and the attachment is protected, so the volume migration that
    implies surfaces as a refusal rather than as a silent break at cutover.
    """

    node: str | FollowsDedicatedVip
    size_gb: int
    mount: str

    @property
    def attached_node(self) -> str:
        """The node this volume attaches to, with the sentinel resolved."""
        return DEDICATED_VIP_NODE if isinstance(self.node, FollowsDedicatedVip) else self.node


#: Every block volume on the fleet, by the name its mount and its node label
#: carry. Volumes are spread one per node: the disk selection in machine
#: configuration stays "the disk that is not the boot disk" rather than a
#: discrimination by size or serial, and losing one node stops taking two
#: preserved datasets with it.
#:
#: Both are preserved rather than backed up (storage.md §3.3): one holds a
#: slice of a distributed archive whose redundancy is the network it came from,
#: the other a replica whose full copy is on the NAS and in every client that
#: syncs it. The invariants the type cannot carry — a node the fleet declares,
#: a mount claimed once, at most one volume per node — are held by tests, after
#: the sentinel resolves.
NODE_VOLUMES: Mapping[str, NodeVolumeEntry] = {
    'hath-cache': NodeVolumeEntry(node=FOLLOWS_DEDICATED_VIP, size_gb=50, mount='/var/mnt/hath-cache'),
    'syncthing-replica': NodeVolumeEntry(node='cp2', size_gb=110, mount='/var/mnt/syncthing-replica'),
}

#: Lower Cost (0 VPUs/GB) is the tier the storage budget is written against:
#: neither dataset is a database, and the balanced tier's surcharge buys
#: latency nothing here needs.
NODE_VOLUME_VPUS = 0
