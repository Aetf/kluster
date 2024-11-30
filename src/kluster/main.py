import pulumi
import pulumi_kubernetes as k8s
from pulumi_kubernetes.apps.v1 import Deployment, DeploymentSpecArgs
from pulumi_kubernetes.meta.v1 import LabelSelectorArgs, ObjectMetaArgs
from pulumi_kubernetes.core.v1 import ContainerArgs, PodSpecArgs, PodTemplateSpecArgs


def namespaced(ns: str, *, createNs: bool = True, **providerArgs) -> k8s.Provider:
    '''Create a new k8s provider with default namespace.'''
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


def main() -> None:
    '''Main function called from scripts/stack.py.'''
    app_labels = {'app': 'nginx'}

    deployment = Deployment(
        'nginx',
        spec=DeploymentSpecArgs(
            selector=LabelSelectorArgs(match_labels=app_labels),
            replicas=1,
            template=PodTemplateSpecArgs(
                metadata=ObjectMetaArgs(labels=app_labels),
                spec=PodSpecArgs(containers=[ContainerArgs(name='nginx', image='nginx')]),
            ),
        ),
    )

    pulumi.export('name', deployment.metadata['name'])
