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
declared through, and nothing else. The Kubernetes provider is opened with the
kubeconfig the `physical` stack publishes, read across a StackReference so that
anything but a kubeconfig stops the run (rfc-007 §3.1,
`kluster.lib.k8s.kubeconfig_from`); kluster-ops#487 moves it into this stack's
own configuration once `physical` is under a passphrase of its own (rfc-005
§5.1). The Cloudflare provider is opened with the zones token in this stack's
own configuration, the credential the `dns` stack opens its provider with too
(credentials.md §3).
"""

from __future__ import annotations

import pulumi
import pulumi_cloudflare as cloudflare
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.lib.k8s import kubeconfig_from

#: Where the zones token is read: at the line that builds the provider it
#: configures, and nowhere else (rfc-002 §8.1). The same key as the `dns`
#: stack's, in this project's namespace rather than the provider package's for
#: the reason given there. The zones row's mint writes its one token under
#: this key into both stacks' configuration (credentials.md §3).
CLOUDFLARE_API_TOKEN = 'cloudflareApiToken'


async def main() -> None:
    config = pulumi.Config()
    physical = pulumi.StackReference(
        f'{pulumi.get_organization()}/{pulumi.get_project()}/{conventions.STACK_NAMES.physical}'
    )

    # Read so that anything but a kubeconfig stops the run, for the reason
    # `k8s_base` gives.
    _ = k8s.Provider(f'{conventions.CLUSTER_NAME}-kubernetes', kubeconfig=kubeconfig_from(physical))

    # One provider for every zone, as in `dns`: the token is scoped to the
    # installation's zones as a set, so it belongs to the program rather than
    # to any one application's component (rfc-002 §8.1).
    _ = cloudflare.Provider(
        f'{conventions.CLUSTER_NAME}-cloudflare',
        api_token=config.require_secret(CLOUDFLARE_API_TOKEN),
    )
