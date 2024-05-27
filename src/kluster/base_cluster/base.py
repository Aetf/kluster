from typing import Optional, override

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s
from putils import Component

from kluster.kx import NamespaceProbe

from .nodes import Nodes


class BaseCluster(Component, pulumi_type='kluster:BaseCluster'):
    """Something"""

    def __init__(self, name: str, is_setup_secrets: bool, opts: Optional[pulumi.ResourceOptions]): ...

    nodes: pulumi.Output[Nodes]

    @override
    def setup(self, name: str, *pargs, is_setup_secrets: bool, **kwargs):
        namespace = NamespaceProbe(f'{name}-probe', opts=pulumi.ResourceOptions(parent=self)).namespace

        return {'nodes': Nodes()}
