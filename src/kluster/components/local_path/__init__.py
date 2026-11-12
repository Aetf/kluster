"""local-path-provisioner: the cluster's default StorageClass, a directory on the node a pod is scheduled to, and a class per node volume.

Talos ships no StorageClass, and the one every node can serve is a directory
of its own disk: `conventions.LOCAL_PATH_ROOT`, a Talos user volume on every
node (physical.md §2, storage.md §2). The class binds when a pod is scheduled,
so the volume lands on that pod's node, and it reclaims `Delete`, with a
volume's backup, where it has one, as the undo (storage.md §3.3).

**A node volume is a pool of the same provisioner** (storage.md §6). Each row
of `conventions.NODE_VOLUMES` gets a class of its own,
`conventions.node_volume_class(row)`, whose pool is the row's mount, and the
provisioner's configuration keys its pools by class. A pool is stated for
every node rather than for the volume's: the node's name is Kubernetes', and a
claim of the class lands where its first consumer is scheduled, which selects
the volume's node label. Nothing mounts the volume on any other node, so a pod
placed elsewhere meets what a pod on that node meets with the volume
unmounted: the helper's `DirectoryOrCreate` cannot create inside the kubelet's
read-only `/var/mnt`, and the claim stays pending (physical.md §2).

Such a class reclaims `Retain`, because the dataset is preserved rather than
backed up and has no undo (storage.md §3.3); it names each claim's directory
by its namespace and name (`pathPattern`), so that a claim made again under the
same name finds its directory; and it hands out `local` volumes rather than
`hostPath` ones (`defaultVolumeType`), whose path the kubelet checks at every
start, so a volume that failed to mount leaves the pod pending instead of
writing to the node's disk.

Declared as resources rather than installed from a chart, after the release's
own manifest at the pinned version: its upstream publishes no chart
repository, and the few objects it is read more clearly here than fetched
(rfc-007 §8). Its two pins are images, the provisioner's and the helper pod's,
which the provisioner starts on a node to create and remove a volume's
directory. The provisioner's configuration, the helper pod's template and the
helper's two scripts are files beside this module, rendered into one
ConfigMap the provisioner reads.

Its namespace is `privileged`, with the reason at its declaration: the helper
pods mount the host path they provision (cluster-infra.md §0).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib import templates
from kluster.lib.k8s import LooserLevel, PodSecurity, namespace
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'DEFAULT_CLASS_ANNOTATION',
    'NAMESPACE',
    'NODE_VOLUME_PATH_PATTERN',
    'PROVISIONER',
    'PROVISIONER_NAME',
    'VOLUME_TYPE_ANNOTATION',
    'LocalPathProvisioner',
    'pools',
)

#: The namespace the provisioner and its helper pods run in.
NAMESPACE = 'local-path-storage'

#: The name of the provisioner's Deployment, its account and its permissions.
PROVISIONER_NAME = 'local-path-provisioner'

#: The provisioner's name, which a StorageClass names to be served by it: the
#: release's default, since nothing sets another.
PROVISIONER = 'rancher.io/local-path'

#: The annotation that makes a StorageClass the one a claim naming none gets.
DEFAULT_CLASS_ANNOTATION = 'storageclass.kubernetes.io/is-default-class'

#: The annotation a class names the type of volume it hands out by, `local` or
#: `hostPath`; an annotation and not a parameter at the pinned v0.0.37
#: (`README.md`, "Storage classes").
VOLUME_TYPE_ANNOTATION = 'defaultVolumeType'

#: The directory of a node volume's claim inside its pool, a Go template over
#: the claim (`README.md` at v0.0.37, "Storage classes"): its namespace and its
#: name, so that a claim made again under the same name finds its directory.
NODE_VOLUME_PATH_PATTERN = '{{ .PVC.Namespace }}/{{ .PVC.Name }}/'

#: Where the provisioner's health server listens, which its probes ask.
HEALTH_PORT = 8080

_LABELS = {'app': PROVISIONER_NAME}


@dataclass(frozen=True, kw_only=True)
class _Configuration:
    """What the files beside this module are rendered with."""

    pools: Mapping[str, str]
    """Each class the provisioner serves, with the node directory a volume of it is created under."""
    image: str
    """The helper pod's image, as the whole reference it is pulled by."""


def pools() -> dict[str, str]:
    """Each class the provisioner serves, with its pool: `local-path` at the user volume, and a node volume's at its mount."""
    return {
        conventions.SC_LOCAL_PATH: conventions.LOCAL_PATH_ROOT,
        **{
            conventions.node_volume_class(row): conventions.node_volume_mount(row)
            for row in sorted(conventions.NODE_VOLUMES)
        },
    }


class LocalPathProvisioner(Component, pulumi_type='kluster:local_path:LocalPathProvisioner'):
    """The provisioner, its permissions and configuration, and the StorageClasses it serves.

    `after` is what the provisioner is installed behind. It is stated on the
    children rather than left to the component's own `depends_on`, which
    Pulumi does not push down to a component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        provisioner_image: ImagePin,
        helper_image: ImagePin,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(
            f'{name}-namespace',
            name=NAMESPACE,
            looser=LooserLevel(
                level=PodSecurity.PRIVILEGED,
                reason='the helper pods mount the host path they provision, which `baseline` refuses',
            ),
            opts=behind,
        )
        named = k8s.meta.v1.ObjectMetaArgs(name=PROVISIONER_NAME, namespace=self.namespace.metadata.name)

        self.service_account = k8s.core.v1.ServiceAccount(name, metadata=named, opts=behind)
        subject = k8s.rbac.v1.SubjectArgs(
            kind='ServiceAccount', name=self.service_account.metadata.name, namespace=self.namespace.metadata.name
        )
        # The helper pods, which the provisioner creates in its own namespace
        # and nowhere else.
        role = k8s.rbac.v1.Role(
            name,
            metadata=named,
            rules=[
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[''],
                    resources=['pods'],
                    verbs=['get', 'list', 'watch', 'create', 'patch', 'update', 'delete'],
                )
            ],
            opts=behind,
        )
        _ = k8s.rbac.v1.RoleBinding(
            name,
            metadata=named,
            role_ref=k8s.rbac.v1.RoleRefArgs(
                api_group='rbac.authorization.k8s.io', kind='Role', name=role.metadata.name
            ),
            subjects=[subject],
            opts=behind,
        )
        cluster_role = k8s.rbac.v1.ClusterRole(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=PROVISIONER_NAME),
            rules=[
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[''],
                    resources=['nodes', 'persistentvolumeclaims', 'configmaps', 'pods', 'pods/log'],
                    verbs=['get', 'list', 'watch'],
                ),
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=[''],
                    resources=['persistentvolumes'],
                    verbs=['get', 'list', 'watch', 'create', 'patch', 'update', 'delete'],
                ),
                k8s.rbac.v1.PolicyRuleArgs(api_groups=[''], resources=['events'], verbs=['create', 'patch']),
                k8s.rbac.v1.PolicyRuleArgs(
                    api_groups=['storage.k8s.io'], resources=['storageclasses'], verbs=['get', 'list', 'watch']
                ),
            ],
            opts=behind,
        )
        _ = k8s.rbac.v1.ClusterRoleBinding(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=PROVISIONER_NAME),
            role_ref=k8s.rbac.v1.RoleRefArgs(
                api_group='rbac.authorization.k8s.io', kind='ClusterRole', name=cluster_role.metadata.name
            ),
            subjects=[subject],
            opts=behind,
        )

        self.storage_class = k8s.storage.v1.StorageClass(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=conventions.SC_LOCAL_PATH, annotations={DEFAULT_CLASS_ANNOTATION: 'true'}
            ),
            provisioner=PROVISIONER,
            reclaim_policy='Delete',
            volume_binding_mode='WaitForFirstConsumer',
            opts=behind,
        )
        self.node_volume_classes = {
            row: k8s.storage.v1.StorageClass(
                f'{name}-{row}',
                metadata=k8s.meta.v1.ObjectMetaArgs(
                    name=conventions.node_volume_class(row), annotations={VOLUME_TYPE_ANNOTATION: 'local'}
                ),
                provisioner=PROVISIONER,
                parameters={'pathPattern': NODE_VOLUME_PATH_PATTERN},
                reclaim_policy='Retain',
                volume_binding_mode='WaitForFirstConsumer',
                opts=behind,
            )
            for row in sorted(conventions.NODE_VOLUMES)
        }

        # Read by name, through the API, once at start: the provisioner's
        # configuration and the helper pod's template, and the helper's scripts
        # the provisioner mounts into each helper pod. Autonamed, so a changed
        # configuration is a new ConfigMap and the Deployment, which names it,
        # rolls onto it.
        self.config = k8s.core.v1.ConfigMap(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
            data=dict(
                templates.render_tree(
                    __name__,
                    'templates',
                    _Configuration(pools=pools(), image=str(helper_image)),
                )
            ),
            opts=behind,
        )

        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=named,
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=_LABELS),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=_LABELS),
                    spec=k8s.core.v1.PodSpecArgs(
                        service_account_name=self.service_account.metadata.name,
                        containers=[self._container(provisioner_image)],
                    ),
                ),
            ),
            opts=behind,
        )

        self.register_outputs({})

    def _container(self, image: ImagePin) -> k8s.core.v1.ContainerArgs:
        """The provisioner's container, which reads its ConfigMap by name."""
        health = k8s.core.v1.HTTPGetActionArgs(path='/health', port='health')
        return k8s.core.v1.ContainerArgs(
            name=PROVISIONER_NAME,
            image=str(image),
            image_pull_policy='IfNotPresent',
            command=[
                'local-path-provisioner',
                '--debug',
                'start',
                '--configmap-name',
                self.config.metadata.name,
            ],
            ports=[k8s.core.v1.ContainerPortArgs(name='health', container_port=HEALTH_PORT, protocol='TCP')],
            startup_probe=k8s.core.v1.ProbeArgs(
                http_get=health, initial_delay_seconds=5, period_seconds=5, failure_threshold=12
            ),
            liveness_probe=k8s.core.v1.ProbeArgs(http_get=health, initial_delay_seconds=10, period_seconds=10),
            readiness_probe=k8s.core.v1.ProbeArgs(
                http_get=k8s.core.v1.HTTPGetActionArgs(path='/ready', port='health'),
                initial_delay_seconds=5,
                period_seconds=5,
            ),
            env=[
                k8s.core.v1.EnvVarArgs(
                    name='POD_NAMESPACE',
                    value_from=k8s.core.v1.EnvVarSourceArgs(
                        field_ref=k8s.core.v1.ObjectFieldSelectorArgs(field_path='metadata.namespace')
                    ),
                ),
                # The account the helper pods run under: the provisioner's
                # own, read off the pod rather than spelled twice.
                k8s.core.v1.EnvVarArgs(
                    name='SERVICE_ACCOUNT_NAME',
                    value_from=k8s.core.v1.EnvVarSourceArgs(
                        field_ref=k8s.core.v1.ObjectFieldSelectorArgs(field_path='spec.serviceAccountName')
                    ),
                ),
                k8s.core.v1.EnvVarArgs(name='HEALTH_PORT', value=str(HEALTH_PORT)),
            ],
        )
