"""The `k8s-base` stack: everything cluster-scoped that speaks the k8s API.

Gateway API CRDs, Cilium, sealed-secrets, cert-manager, CNPG and VolSync,
VictoriaMetrics, NFD and the GPU plugin, and the small standing set the legacy
cluster proved — in that dependency order, per
docs/declarative/cluster-infra.md. The component list is closed: additions
argue for themselves in writing first.

Its components live in areas of `kluster/components/`, the way `physical`
composes the areas it declares: this module stays the wiring, one component per
entry of the closed list, each handed what it is installed behind; those
edges are the dependency chain that lets one `up` converge from an empty
cluster. What every component shares — installing a pinned chart,
creating a namespace at its Pod Security level, sealing a secret, labeling a
Service into a load-balancer pool — is in `kluster.lib.k8s`.

The program reads two things beside the pins. The cluster-admin kubeconfig
opens the one Kubernetes provider every component is declared through, and is
a config secret of this stack's own (rfc-007 §3.1). The `physical` stack
generates it, and it cannot cross the stack boundary by StackReference, the way
the machine facts do (declarative/README.md §2): `physical` is encrypted under
a passphrase of its own (rfc-005 §5.1), and a StackReference elides the secrets
the reading stack cannot decrypt. So `credentials derived sync --only
kubeconfig` copies it out of `physical`'s state into this stack's
configuration, and it is read so that anything but a kubeconfig stops the run
(`kluster.lib.k8s.kubeconfig_from`). The addresses the `internet` pool is made
of are plain machine facts, and they do cross by StackReference: the cloud
nodes' private IPv4s and GUAs, the dedicated VIP's private address and the
balancer's two public ones (rfc-007 §4.4), each read so that anything but an
address stops the run (`kluster.lib.stack_addresses`).

The chart set is pinned on first contact (declarative/README.md, "Deliberately
not pre-decided"), and each chart is a project-level `versions:chart-<name>`
pin in `Pulumi.yaml`'s `config:` block, which renovate moves
(framework/pulumi.md §3.2), as the Gateway API definitions are a
`versions:manifest-<name>` pin and local-path-provisioner's two images are
`versions:image-<name>` pins; a component installs what the program reads
through `kluster.lib.versions` and hands it. The custom resources are written
against `sdks/crds`, which `mise x -- uv run update_crds` regenerates from the
same pins.
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.backup.volsync import VolSync
from kluster.components.certificates import CertManager
from kluster.components.cilium import Cilium, InternetPoolMembers
from kluster.components.gpu import IntelGpuPlugin, NodeFeatureDiscovery
from kluster.components.local_path import LocalPathProvisioner
from kluster.components.postgres import PostgresOperator
from kluster.components.reloader import Reloader
from kluster.components.sealing import SealedSecretsController
from kluster.lib import stack_addresses
from kluster.lib.k8s import KUBECONFIG_KEY, kubeconfig_from
from kluster.lib.release_assets import fetch_manifest
from kluster.lib.versions import versions
from putils import background


async def main() -> None:
    config = pulumi.Config()

    # Fetched before anything is declared: it reads no output, and it is a
    # release asset, refused unless its bytes are the pin's.
    gateway_api_definitions = await background(fetch_manifest)(versions.manifest['gateway-api'])

    # Read so that anything but a kubeconfig stops the run, naming what it
    # found: no copy in this stack's configuration, or one that is blank or
    # the unknown sentinel a targeted apply exports. The provider would read
    # each as an unreachable cluster -- plain resources previewing green by
    # echoing their inputs -- or, handed nothing, fall back to the shell's
    # `$KUBECONFIG`.
    provider = k8s.Provider(
        f'{conventions.CLUSTER_NAME}-kubernetes',
        kubeconfig=kubeconfig_from(config.get_secret(KUBECONFIG_KEY)),
    )
    physical = pulumi.StackReference(
        f'{pulumi.get_organization()}/{pulumi.get_project()}/{conventions.STACK_NAMES.physical}'
    )

    opts = pulumi.ResourceOptions(providers=[provider])

    # cluster-infra.md §1's order. Nothing schedules before the CNI, so
    # everything is behind Cilium; cert-manager is behind the sealing
    # controller, because the credential its solver uses is a sealed value.
    # A chart that declares objects another chart defines is behind that
    # chart: the database operator's backup plugin and the GPU plugin's
    # operator declare cert-manager's, and the GPU plugin declares NFD's.
    cilium = Cilium(
        'cilium',
        chart=versions.chart['cilium'],
        gateway_api_definitions=gateway_api_definitions,
        internet_pool=_internet_pool_members(physical),
        opts=opts,
    )
    sealing = SealedSecretsController(
        'sealed-secrets', chart=versions.chart['sealed-secrets'], after=[cilium], opts=opts
    )
    cert_manager = CertManager('cert-manager', chart=versions.chart['cert-manager'], after=[sealing], opts=opts)
    _ = PostgresOperator(
        'cloudnative-pg',
        operator_chart=versions.chart['cloudnative-pg'],
        barman_cloud_chart=versions.chart['plugin-barman-cloud'],
        after=[cert_manager],
        opts=opts,
    )
    _ = VolSync('volsync', chart=versions.chart['volsync'], after=[cert_manager], opts=opts)
    node_features = NodeFeatureDiscovery(
        'node-feature-discovery', chart=versions.chart['node-feature-discovery'], after=[cilium], opts=opts
    )
    _ = IntelGpuPlugin(
        'intel-device-plugins',
        operator_chart=versions.chart['intel-device-plugins-operator'],
        gpu_chart=versions.chart['intel-device-plugins-gpu'],
        after=[cert_manager, node_features],
        opts=opts,
    )
    _ = LocalPathProvisioner(
        'local-path',
        provisioner_image=versions.image['local-path-provisioner'],
        helper_image=versions.image['local-path-helper'],
        after=[cilium],
        opts=opts,
    )
    _ = Reloader('reloader', chart=versions.chart['reloader'], after=[cilium], opts=opts)


def _internet_pool_members(physical: pulumi.StackReference) -> InternetPoolMembers:
    """The `internet` pool's members, out of the physical stack, each refused unless it is an address.

    These are the only outputs this program reads across the reference. The
    names asked for are `conventions.PHYSICAL_OUTPUTS`, the structure
    `physical` exports under, so a rename there is a rename here in the same
    edit. Each read checks what it got, unknowns included: a targeted apply of
    `physical` can leave any of these as Pulumi's unknown sentinel, which reads
    back absent or unknown, and a pool built from that would carry `"None"`
    as an address (framework/pulumi.md §1.4).
    """
    outputs = conventions.PHYSICAL_OUTPUTS
    return InternetPoolMembers(
        node_private_ips=stack_addresses.addresses_by_name(physical, outputs.node_private_ips, 4),
        node_guas=stack_addresses.addresses_by_name(physical, outputs.node_guas, 6),
        dedicated_vip=stack_addresses.address(physical, outputs.vip1_private, 4),
        balancer_v4=stack_addresses.address(physical, outputs.cluster_endpoint, 4),
        balancer_v6=stack_addresses.address(physical, outputs.cluster_endpoint_v6, 6),
    )
