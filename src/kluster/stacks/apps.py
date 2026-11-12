"""The `apps` stack: the applications and everything that travels with them.

Each app component owns its namespace, workload, storage and backups, its
exposure (routes, gateways) and its DNS records — the contract in
docs/declarative/workloads.md. This is the daily driver: most deployments
touch only this stack, which is why it reaches nothing on the LAN. It reaches
the cluster, for the applications, and Cloudflare, for the public records that
are declared beside each of them (dns.md §1); the split-horizon rewrites its
routes imply are applied by `dns`, from the same plain-data declaration (dns.md
§3).

What the program builds today is the two providers every application will be
declared through, each opened with a credential in this stack's own
configuration, and the one read across a StackReference its applications are
handed: the addresses of a raw port's `internet` fronts, which are `physical`'s
outputs -- the `internet` pool's members and the worker's GUA -- each read so
that anything but an address stops the run (`kluster.lib.stack_addresses`,
rfc-008 §4.1). No application's component is wired yet: each is wired here as
it lands (sources-of-truth.md §3), handed its `Exposure` and these fronts,
and the read stands ahead of the first so that a `physical` that cannot
supply them stops this stack's run rather than that land's. The Kubernetes provider is opened with the
cluster-admin kubeconfig, which `credentials derived sync --only kubeconfig`
copies there out of the `physical` stack's state, a StackReference being unable
to carry a secret across `physical`'s own passphrase (rfc-005 §5.1); it is read
so that anything but a kubeconfig stops the run (rfc-007 §3.1,
`kluster.lib.k8s.kubeconfig_from`). The Cloudflare provider is opened with the
zones token, the credential the `dns` stack opens its provider with too
(credentials.md §3).
"""

from __future__ import annotations

import pulumi
import pulumi_cloudflare as cloudflare
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Fronts
from kluster.components.cilium import internet_pool_members
from kluster.lib import stack_addresses
from kluster.lib.k8s import KUBECONFIG_KEY, kubeconfig_from

#: Where the zones token is read: at the line that builds the provider it
#: configures, and nowhere else (rfc-002 §8.1). The same key as the `dns`
#: stack's, in this project's namespace rather than the provider package's for
#: the reason given there. The zones row's mint writes its one token under
#: this key into both stacks' configuration (credentials.md §3).
CLOUDFLARE_API_TOKEN = 'cloudflareApiToken'


async def main() -> None:
    config = pulumi.Config()

    # Read so that anything but a kubeconfig stops the run, for the reason
    # `k8s_base` gives.
    _ = k8s.Provider(
        f'{conventions.CLUSTER_NAME}-kubernetes',
        kubeconfig=kubeconfig_from(config.get_secret(KUBECONFIG_KEY)),
    )

    # One provider for every zone, as in `dns`: the token is scoped to the
    # installation's zones as a set, so it belongs to the program rather than
    # to any one application's component (rfc-002 §8.1).
    _ = cloudflare.Provider(
        f'{conventions.CLUSTER_NAME}-cloudflare',
        api_token=config.require_secret(CLOUDFLARE_API_TOKEN),
    )

    physical = pulumi.StackReference(
        f'{pulumi.get_organization()}/{pulumi.get_project()}/{conventions.STACK_NAMES.physical}'
    )
    _ = _fronts(physical)


def _fronts(physical: pulumi.StackReference) -> Fronts:
    """The addresses of a raw port's `internet` fronts, out of the physical stack, each refused unless it is an address.

    These are the only outputs this program reads across the reference, and
    each is a machine fact a Service is declared at (style/pulumi.md,
    "Layering"): the `internet` pool's members, as `k8s-base` reads them, and
    the worker's GUA, beside its IPv4, which is a convention.
    """
    return Fronts(
        internet=internet_pool_members(physical),
        worker_gua=stack_addresses.address(physical, conventions.PHYSICAL_OUTPUTS.worker_gua, 6),
    )
