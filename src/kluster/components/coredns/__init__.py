"""The cluster's DNS: a replica on each site, and the `kube-dns` Service preferring the one at its client's site.

CoreDNS is Talos' bootstrap manifest (`k8stemplates/coredns.go` at v1.13.9,
commit `3ebd10a7c1bd`): the `coredns` Deployment, L217–389, and the
`kube-dns` Service, L23–87, both in `kube-system`. Nothing in that manifest
is about sites: beside the OS label, its replicas are placed by a
*preferred* anti-affinity over the hostname alone, L251–274, so both can be
on control planes. With the home uplink down, a pod on the worker reaches
no cloud node, so with both replicas in the cloud every lookup it sends
fails (cluster/architecture.md §1.1). This component adds what the manifest
lacks: a spread of the replicas over `conventions.SITE_LABEL`, so each site
holds one, and `trafficDistribution: PreferSameZone` on the Service, so a
client resolves at the replica on its own site while that replica serves,
with the uplink up and with it down, and at any replica while none at its
site does (Cilium's `pkg/loadbalancer/writer/writer.go` L582–616 at
v1.20.4). The zone hints the EndpointSlice controller writes for that
distribution are honored by Cilium's `loadBalancer.serviceTopology`
(`components.cilium`). The zone is the site: the EndpointSlice controller
reads a node's zone from the label `SITE_LABEL` names.

**It is a layer over Talos' objects, not the manifest taken over.** Each
object is patched by server-side apply (`DeploymentPatch`, `ServicePatch`)
under a field manager of its own, `FIELD_MANAGER`, and the patch names only
fields the manifest does not set. Talos then keeps writing every field it
owns, and nothing here restates one of them:

- **machined applies a bootstrap manifest only while its object is
  missing.** An object already in its inventory is skipped, and one that
  exists is added to the inventory and skipped
  (`controllers/k8s/manifest_apply.go` L335–347).
- **`talosctl upgrade-k8s` applies the manifest again by server-side apply**,
  as the field manager `talos` (`pkg/cluster/kubernetes/talos_managed.go`
  L163–165 and L511–566; `constants.go` L1134–1135), the path it takes
  when every node runs Talos 1.13 or later
  (`compatibility/talos_version.go` L144–147). A server-side apply removes
  only fields its own manager held and the applied object omits
  (https://kubernetes.io/docs/reference/using-api/server-side-apply/), so
  the fields this patch owns, which Talos' object never names, stay as
  they are.
- **If a later manifest sets one of these fields**, Talos' apply takes it:
  it goes through Flux's server-side apply with forced ownership
  (`fluxcd/pkg` `ssa/manager_apply.go` L370–376 at `ssa/v0.73.0`, the
  version `go-kubernetes` v0.2.38 requires), so Talos' value replaces this
  one and the field moves to `talos`. The patch does not force
  (`pulumi.com/patchForce` is unset), so when it is next applied it fails
  on the conflict with `talos`, naming the field, rather than taking the
  field back. Which side should own it is then a decision to make, not
  one either side makes in silence.
- **An object Talos recreates comes back without the patch.** machined
  recreates a deleted object, and `upgrade-k8s` with forced manifests
  deletes and recreates one whose change touches an immutable field
  (`fluxcd/pkg` `ssa/manager_apply.go` L132 and L210). The next `k8s-base`
  apply, whose refresh finds the fields gone, applies the patch again.

The layer is this module and the line in `k8s-base` that declares it.
Deleting the component releases the patched fields, which return to their
defaults, and Talos' objects stand as its manifest writes them.

**The replicas are the manifest's.** It pins two (L232), and nothing here
sets a count. With the worker the one node at the home site and the
control planes in the cloud, `maxSkew: 1` and `DoNotSchedule` keep exactly
one replica at each site. When the worker is down, the home replica is not
placed at the cloud site instead: it stays `Pending` until the worker
returns, because a node that cannot take the pod still counts as its site
(`nodeTaintsPolicy: Ignore`), and a second replica in the cloud would put
the sites two apart. The cloud replica keeps running, since a spread
constrains only where a new pod is placed. The patch waits for the
Deployment's rollout, as a Deployment's apply does, so a patch applied
while the worker is down fails on that wait rather than reporting a
rollout that has not finished. `talosctl upgrade-k8s` waits for what it
changes to reconcile, failing fast (`talos_managed.go` L574–580), so an upgrade that changes CoreDNS, run while
the worker is down, fails that wait on the home replica.

**A rollout ends one replica per site.** The spread counts only the pods
of the replica set being rolled out (`matchLabelKeys: [pod-template-hash]`),
so the new set's two pods go to different sites whichever old pod the
rollout removes first. Counting the old pods too, a rollout can end with
both new pods in the cloud: the old cloud pod goes first, the new one
replacing it at the cloud site, and the next new pod then fits the cloud
too, beside the old home pod, which goes last.
"""

from __future__ import annotations

from collections.abc import Sequence

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from putils import Component

__all__ = (
    'DEPLOYMENT',
    'FIELD_MANAGER',
    'NAMESPACE',
    'POD_LABELS',
    'SERVICE',
    'CoreDnsPerSite',
)

#: The namespace Talos' manifest puts CoreDNS in.
NAMESPACE = 'kube-system'

#: The Deployment Talos' manifest names CoreDNS, and the Service in front of it.
DEPLOYMENT = 'coredns'
SERVICE = 'kube-dns'

#: The label Talos' manifest puts on CoreDNS's pods (`coredns.go` L246–248):
#: the pods the spread counts.
POD_LABELS = {'k8s-app': 'kube-dns'}

#: The field manager both patches apply as, which is what a reader of an
#: object's `managedFields` sees owning the fields this layer sets.
FIELD_MANAGER = 'kluster-coredns'

#: The annotation the Kubernetes provider reads a patch's field manager from.
_PATCH_FIELD_MANAGER = 'pulumi.com/patchFieldManager'

#: The label the Deployment controller puts on each replica set's pods, a
#: hash of the pod template: the spread counts one replica set at a time.
_POD_TEMPLATE_HASH = 'pod-template-hash'


class CoreDnsPerSite(Component, pulumi_type='kluster:coredns:CoreDnsPerSite'):
    """CoreDNS spread one replica per site, and `kube-dns` preferring the replica at its client's site.

    `after` is what the patches are applied behind: Cilium, without which
    no pod of the Deployment's rollout becomes ready. It is stated on each
    child rather than left to the component's own `depends_on`, which
    Pulumi does not push down to a component's children.
    """

    def __init__(
        self,
        name: str,
        *,
        after: Sequence[pulumi.Resource],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        def metadata(object_name: str) -> k8s.meta.v1.ObjectMetaPatchArgs:
            # No `pulumi.com/patchForce`: a conflict with the manager `talos`
            # must fail the apply, not be taken (module docstring).
            return k8s.meta.v1.ObjectMetaPatchArgs(
                name=object_name,
                namespace=NAMESPACE,
                annotations={_PATCH_FIELD_MANAGER: FIELD_MANAGER},
            )

        self.deployment = k8s.apps.v1.DeploymentPatch(
            name,
            metadata=metadata(DEPLOYMENT),
            spec=k8s.apps.v1.DeploymentSpecPatchArgs(
                template=k8s.core.v1.PodTemplateSpecPatchArgs(
                    spec=k8s.core.v1.PodSpecPatchArgs(
                        topology_spread_constraints=[
                            k8s.core.v1.TopologySpreadConstraintPatchArgs(
                                topology_key=conventions.SITE_LABEL,
                                max_skew=1,
                                when_unsatisfiable='DoNotSchedule',
                                label_selector=k8s.meta.v1.LabelSelectorPatchArgs(match_labels=POD_LABELS),
                                match_label_keys=[_POD_TEMPLATE_HASH],
                                # The default, stated: a site whose node cannot
                                # take the pod still counts, so its replica
                                # waits there rather than doubling up at the
                                # other site.
                                node_taints_policy='Ignore',
                            )
                        ],
                    ),
                ),
            ),
            opts=self.child_opts(depends_on=list(after)),
        )

        self.service = k8s.core.v1.ServicePatch(
            name,
            metadata=metadata(SERVICE),
            spec=k8s.core.v1.ServiceSpecPatchArgs(traffic_distribution='PreferSameZone'),
            opts=self.child_opts(depends_on=list(after)),
        )

        self.register_outputs({})
