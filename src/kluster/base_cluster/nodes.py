from typing import Mapping, Optional

import pulumi
import pulumi_kubernetes as k8s

from kluster.kx import Node


def _create_node(hostname: str, labels: Optional[Mapping[str, str]]) -> Node:
    return Node(hostname, metadata=k8s.meta.v1.ObjectMetaPatchArgs(name=hostname, labels=labels))


class Nodes:
    AetfArchVPS: Node = _create_node(
        'aetf-arch-vps',
        labels={
            'svccontroller.k3s.cattle.io/lbpool': 'internet',
            'svccontroller.k3s.cattle.io/enablelb': 'true',
        },
    )
    AetfArchHomelab: Node = _create_node(
        'aetf-arch-homelab',
        labels={
            'svccontroller.k3s.cattle.io/lbpool': 'homelab',
            'svccontroller.k3s.cattle.io/enablelb': 'true',
        },
    )
