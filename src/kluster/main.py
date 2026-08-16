import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes.meta.v1 import ObjectMetaArgs

from . import physical


def namespaced(ns: str, *, createNs: bool = True, **providerArgs) -> k8s.Provider:
    """Create a new k8s provider with default namespace."""
    if createNs:
        k8s.core.v1.Namespace(
            ns, metadata=ObjectMetaArgs(name=ns), opts=pulumi.ResourceOptions(delete_before_replace=True)
        )

    providerArgs = {
        **providerArgs,
        # Provider is only replaced when this identifer changes
        'cluster_identifier': f'kluster-{ns}',
        'enable_server_side_apply': True,
        'namespace': ns,
        'suppress_deprecation_warnings': True,
        'suppress_helm_hook_warnings': True,
    }
    return k8s.Provider(f'{ns}-provider', **providerArgs)


async def main() -> None:
    """Program entrypoint, awaited on the Pulumi event loop via pulumi.run().

    Async pre-work (external APIs, stack output details, file reads) can be
    awaited here directly before or between resource declarations.
    """
    physical.setup()
