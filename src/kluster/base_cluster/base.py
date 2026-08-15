from typing import Optional

import pulumi
from putils import Component

from kluster.kx import NamespaceProbe

from .nodes import Nodes


class BaseCluster(Component, pulumi_type='kluster:BaseCluster'):
    """Something"""

    nodes: Nodes

    def __init__(self, name: str, is_setup_secrets: bool, opts: Optional[pulumi.ResourceOptions] = None):
        super().__init__(name, opts=opts)
        self.probe = NamespaceProbe(f'{name}-probe', opts=self.child_opts())
        self.nodes = Nodes()
        self.register_outputs({})
