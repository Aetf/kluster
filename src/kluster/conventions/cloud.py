"""The cloud site: the node fleet, its sizing, its network plan, its per-node capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Network

from kluster.conventions.homelab import HOMELAB_NODE
from kluster.conventions.identity import LABEL_DOMAIN

#: Three combined control-plane/ingress nodes (architecture.md §1.1).
CLOUD_NODES = ('cp1', 'cp2', 'cp3')

#: Every Talos node this program declares.
ALL_NODES = (*CLOUD_NODES, HOMELAB_NODE)

#: Two OCPUs per node: the fewest cores Talos documents for a control plane,
#: and the low end of what etcd's hardware guide gives a typical cluster. One
#: core carries the steady state but not a burst of creates, under which etcd
#: on every member missed its deadlines (nodes.md §1). The fleet bills past the
#: A1 free allowance, which is the same for every tenancy; what its month costs
#: is nodes.md §3.2, and the budget is set from it (`components/cloud/guardrails.py`).
NODE_OCPUS = 2
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
    """A block volume attached to one node, keyed in `NODE_VOLUMES` by its name.

    `node` names a node of the fleet, or `FOLLOWS_DEDICATED_VIP` where the
    dataset belongs on whichever node carries the VIP — a workload whose
    traffic must leave by the address it arrives on, with its cache pinned to
    that machine. The sentinel is what makes "the VIP moved, the volume stayed"
    a state nobody can write; a `DEDICATED_VIP_NODE` edit re-declares the
    attachment, and the attachment is protected, so the volume migration that
    implies surfaces as a refusal rather than as a silent break at cutover.

    The entry carries no path, because the name is the dataset's identity in
    every layer it passes through: the OCI volume's logical name, the Talos
    user volume and the partition label Talos writes for it (`u-<name>`),
    the path Talos mounts it at (`node_volume_mount`), and the value of the
    node's `NODE_VOLUME_LABEL`. Renaming a row therefore renames the OCI
    volume, which is a delete of a protected resource that Pulumi refuses; a
    rename is a migration — move the data, then the row — and never an edit.
    """

    node: str | FollowsDedicatedVip
    size_gb: int

    @property
    def attached_node(self) -> str:
        """The node this volume attaches to, with the sentinel resolved."""
        return DEDICATED_VIP_NODE if isinstance(self.node, FollowsDedicatedVip) else self.node


#: Every block volume on the fleet, by the name that identifies its dataset
#: (`NodeVolumeEntry`). Volumes are spread one per node: the disk selection in
#: machine configuration stays "the disk that is not the boot disk" rather
#: than a discrimination by size or serial, and losing one node takes at most
#: one preserved dataset with it.
#:
#: The hath cache is preserved rather than backed up (storage.md §3.3): it is
#: a slice of a distributed archive whose redundancy is the network it came
#: from. It is exactly 50 GB of data, and its volume is sized above that. The
#: invariants the type cannot carry are held by `tests/test_conventions.py`,
#: after the sentinel resolves.
NODE_VOLUMES: Mapping[str, NodeVolumeEntry] = {
    'hath-cache': NodeVolumeEntry(node=FOLLOWS_DEDICATED_VIP, size_gb=55),
}

#: Where Talos mounts every user volume: its `constants.UserVolumeMountPoint`.
#: A fact about Talos rather than a decision of this program — a user volume
#: is mounted at `<root>/<name>` and nowhere else, and the machine
#: configuration has no field that could move it.
USER_VOLUME_ROOT = '/var/mnt'


def node_volume_mount(name: str) -> str:
    """The path a node volume is mounted at on its node, from the row's name.

    The pool of the row's `local-path` class (`cluster.node_volume_class`),
    which hands out a directory *inside* this path for each claim, never the
    path itself (storage.md §6).
    """
    return f'{USER_VOLUME_ROOT}/{name}'


#: The node label naming the volume a node carries, with the row's name as its
#: value. The row's class (`cluster.node_volume_class`) allows it as its one
#: topology, so the scheduler places every consumer of the class on the
#: volume's node, and a consumer needs no node selection of its own
#: (storage.md §6). Placement is thereby a fact about the node rather than a
#: string a workload repeats (rfc-002 §10.5). One key is enough because a node
#: carries at most one volume.
NODE_VOLUME_LABEL = f'{LABEL_DOMAIN}/node-volume'

#: Lower Cost (0 VPUs/GB) is the tier the storage budget is written against:
#: the dataset is no database, and the balanced tier's surcharge buys latency
#: nothing here needs.
NODE_VOLUME_VPUS = 0
