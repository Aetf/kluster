"""The `k8s-base` stack: everything cluster-scoped that speaks the k8s API.

Gateway API CRDs, Cilium, sealed-secrets, cert-manager, CNPG and VolSync with
the volume snapshot definitions it watches, VictoriaMetrics, NFD and the GPU
plugin, the small standing set the legacy cluster proved, and the hourly etcd
snapshot — in that dependency order, per
docs/declarative/cluster-infra.md. The component list is closed: additions
argue for themselves in writing first.

Its components live in areas of `kluster/components/`, the way `physical`
composes the areas it declares: this module stays the wiring, a component for
each thing the closed list installs -- an entry is one component, or one per
part where its parts are installed separately -- each handed what it is
installed behind; those edges are the dependency chain that lets one `up`
converge from an empty cluster. Cilium's BGP session is such a part: its
password is a sealed value and the sealing controller is installed behind
Cilium. What every component shares — installing a pinned chart,
creating a namespace at its Pod Security level, sealing a secret, labeling a
Service into a load-balancer pool — is in `kluster.lib.k8s`.

Beside the pins, the program reads what follows. The cluster-admin kubeconfig
opens the one Kubernetes provider every component is declared through, and is
a config secret of this stack's own (rfc-007 §3.1). The `physical` stack
generates it, and it cannot cross the stack boundary by StackReference, the way
the machine facts do (declarative/README.md §2): `physical` is encrypted under
a passphrase of its own (rfc-005 §5.1), and a StackReference elides the secrets
the reading stack cannot decrypt. So `credentials derived sync --only
kubeconfig` copies it out of `physical`'s state into this stack's
configuration, and it is read so that anything but a kubeconfig stops the run
(`kluster.lib.k8s.kubeconfig_from`). The expiry of the client certificate it
carries is read off it too, and a kubeconfig that authenticates any other
way is refused (`kluster.lib.k8s.client_certificate_expiry`): monitoring
alerts ahead of that date, after which the copy no longer opens the cluster
(operations.md §2.5). Beside it is the random provider, which
reads nothing and generates what a component keeps in state, such as Grafana's
admin password. The addresses the pools are made of are
plain machine facts, and they do cross by StackReference: for the `internet`
pool, the cloud nodes' private IPv4s and GUAs, the dedicated VIP's private
address and the balancer's two public ones (rfc-007 §4.4,
`kluster.components.cilium.internet_pool_members`), and for the worker's own
pool, the worker's GUA beside its IPv4, which is a convention (rfc-008 §4.1);
each is read so that anything but an address stops the run
(`kluster.lib.stack_addresses`). The OCIDs of the cloud nodes' volumes cross
the same way, which the pull of OCI's block-volume series names its series
by; they are read only once that pull's key is sealed in, since nothing else
reads them, and so that anything but a map of OCIDs stops the run
(`kluster.lib.stack_volumes`). The age recipients the etcd snapshots are
encrypted to are the escrowed backup generations' committed file, the one the
state backend's dumps are encrypted to (`kluster.lib.state_backend.committed`),
read so that a line that is no recipient stops the run. And the sealed values
it declares are plain values of this stack's configuration, under the one key
`conventions.sealed.CONFIG_KEY`, read out per value by the value's census row
(`kluster.lib.k8s.sealed_ciphertext`) and handed to the component that
declares it (rfc-007 §6.2). One the stack does not hold yet is declared as
nothing, with a warning, until the bring-up's gate has passed
(`kluster.lib.k8s.sealed_value`, physical/bring-up.md §1).

The chart set is pinned on first contact (declarative/README.md, "Deliberately
not pre-decided"), and each chart is a project-level `versions:chart-<name>`
pin in `Pulumi.yaml`'s `config:` block, which renovate moves
(framework/pulumi.md §3.2), as the Gateway API definitions are a
`versions:manifest-<name>` pin, the volume snapshot definitions a
`versions:source-<name>` pin and the images a component runs beside a chart,
local-path-provisioner's two, the monitoring stack's history store, backups,
restores and OCI pull, and the etcd snapshot, `versions:image-<name>` pins; a component installs what the program reads
through `kluster.lib.versions` and hands it. The custom resources are written
against `sdks/crds`, which `mise x -- uv run update_crds` regenerates from the
same pins, but for the VictoriaMetrics operator's, which that SDK cannot import
yet (`kluster.components.monitoring.operator_objects`).
"""

from __future__ import annotations

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random

from kluster import conventions
from kluster.components.backup.etcd import EtcdSnapshots
from kluster.components.backup.snapshots import SnapshotDefinitions
from kluster.components.backup.volsync import VolSync
from kluster.components.certificates import CertManager
from kluster.components.cilium import BgpSession, Cilium, internet_pool_members
from kluster.components.gpu import IntelGpuPlugin, NodeFeatureDiscovery
from kluster.components.local_path import LocalPathProvisioner
from kluster.components.monitoring import Monitoring, OciPull
from kluster.components.postgres import PostgresOperator
from kluster.components.reloader import Reloader
from kluster.components.sealing import SealedSecretsController
from kluster.conventions.backup import BackupFamily
from kluster.lib import stack_addresses, stack_volumes
from kluster.lib.k8s import KUBECONFIG_KEY, client_certificate_expiry, kubeconfig_from, sealed_ciphertext
from kluster.lib.release_assets import fetch_manifest
from kluster.lib.source_files import fetch_source_files
from kluster.lib.state_backend import committed
from kluster.lib.versions import versions
from putils import background


async def main() -> None:
    config = pulumi.Config()

    # Fetched before anything is declared: they read no output, and each is
    # refused unless its bytes are the pin's.
    gateway_api_definitions = await background(fetch_manifest)(versions.manifest['gateway-api'])
    snapshot_definitions = await background(fetch_source_files)(versions.source['external-snapshotter'])

    # Read so that anything but a kubeconfig stops the run, naming what it
    # found: no copy in this stack's configuration, or one that is blank or
    # the unknown sentinel a targeted apply exports. The provider would read
    # each as an unreachable cluster -- plain resources previewing green by
    # echoing their inputs -- or, handed nothing, fall back to the shell's
    # `$KUBECONFIG`.
    kubeconfig = kubeconfig_from(config.get_secret(KUBECONFIG_KEY))
    provider = k8s.Provider(f'{conventions.CLUSTER_NAME}-kubernetes', kubeconfig=kubeconfig)
    # When the copy's client certificate expires, which `KubeconfigCopyExpiring`
    # is built from. Declassified: a certificate's `NotAfter` is in its public
    # half, and the date in the rule is the one trace of the copy a preview can
    # show, moving a year on when a sync's pull request carries a new one.
    kubeconfig_expiry = pulumi.Output.unsecret(kubeconfig.apply(client_certificate_expiry))
    physical = pulumi.StackReference(
        f'{pulumi.get_organization()}/{pulumi.get_project()}/{conventions.STACK_NAMES.physical}'
    )

    # The random provider reads no configuration; it is explicit because this
    # stack's file disables every package's default provider it uses.
    generator = random.Provider(f'{conventions.CLUSTER_NAME}-random')
    opts = pulumi.ResourceOptions(providers=[provider, generator])
    sealed = config.get_object(conventions.sealed.CONFIG_KEY)

    # cluster-infra.md §1's order. Nothing schedules before the CNI, so
    # everything is behind Cilium; cert-manager, the BGP session, monitoring
    # and the etcd snapshot are behind the sealing controller, because the
    # credential cert-manager's solver uses, the session's password, the
    # address alertmanager posts to and the key the metrics stores are backed
    # up with, and the key the snapshots are uploaded with are sealed values.
    # A chart that declares objects another chart defines is behind that
    # chart: the database operator's backup plugin and the GPU plugin's
    # operator declare cert-manager's, and the GPU plugin declares NFD's.
    cilium = Cilium(
        'cilium',
        chart=versions.chart['cilium'],
        gateway_api_definitions=gateway_api_definitions,
        internet_pool=internet_pool_members(physical),
        worker_gua=stack_addresses.address(physical, conventions.PHYSICAL_OUTPUTS.worker_gua, 6),
        exposures=tuple(conventions.routes.EXPOSURES.values()),
        opts=opts,
    )
    sealing = SealedSecretsController(
        'sealed-secrets', chart=versions.chart['sealed-secrets'], after=[cilium], opts=opts
    )
    _ = BgpSession(
        'bgp',
        after=[cilium, sealing],
        password=sealed_ciphertext(conventions.sealed.BGP_PASSWORD, sealed),
        opts=opts,
    )
    cert_manager = CertManager(
        'cert-manager',
        chart=versions.chart['cert-manager'],
        after=[sealing],
        dns01_token=sealed_ciphertext(conventions.sealed.DNS01_TOKEN, sealed),
        certificates=cilium.gateways.certificates,
        certificates_namespace=cilium.gateways.namespace.metadata.name,
        opts=opts,
    )
    _ = PostgresOperator(
        'cloudnative-pg',
        operator_chart=versions.chart['cloudnative-pg'],
        barman_cloud_chart=versions.chart['plugin-barman-cloud'],
        after=[cert_manager],
        opts=opts,
    )
    # VolSync's controller watches volume snapshots, so it is behind their
    # definitions.
    snapshots = SnapshotDefinitions('snapshot-definitions', definitions=snapshot_definitions, after=[cilium], opts=opts)
    _ = VolSync('volsync', chart=versions.chart['volsync'], after=[cert_manager, snapshots], opts=opts)
    # The volumes' OCIDs are read only where the pull is declared, which is
    # once the metrics reader's key is sealed in.
    metrics_reader_key = sealed_ciphertext(conventions.sealed.OCI_METRICS, sealed)
    _ = Monitoring(
        'monitoring',
        stack_chart=versions.chart['victoria-metrics-k8s-stack'],
        metrics_server_chart=versions.chart['metrics-server'],
        after=[cilium, sealing],
        alert_webhook=sealed_ciphertext(conventions.sealed.ALERT_WEBHOOK, sealed),
        kubeconfig_expiry=kubeconfig_expiry,
        writer_key=sealed_ciphertext(
            conventions.sealed.writer_key(conventions.MONITORING_NAMESPACE, BackupFamily.VM), sealed
        ),
        history_image=versions.image['metrics-history'],
        backup_image=versions.image['metrics-backup'],
        restore_image=versions.image['metrics-restore'],
        bucket_image=versions.image['metrics-bucket'],
        oci_pull=OciPull(
            ciphertext=metrics_reader_key,
            volume_ids=stack_volumes.volume_ids(physical, conventions.PHYSICAL_OUTPUTS.volume_ids),
        )
        if metrics_reader_key is not None
        else None,
        oci_sdk_image=versions.image['oci-sdk'],
        opts=opts,
    )
    # Encrypted to the escrowed backup generations, the recipients the state
    # backend's dumps are encrypted to, so one rotation moves both.
    _ = EtcdSnapshots(
        'etcd-snapshots',
        image=versions.image['etcd-backup'],
        writer_key=sealed_ciphertext(conventions.sealed.ETCD_WRITER_KEY, sealed),
        recipients=tuple(committed.backup_recipients().values()),
        after=[cilium, sealing],
        opts=opts,
    )
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
