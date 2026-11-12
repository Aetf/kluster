"""Monitoring: the VictoriaMetrics stack and metrics-server, on the homelab worker, and the in-cluster alert leg.

Item 6 of declarative/cluster-infra.md §1, with metrics-server from item 8
(rfc-007 §7, §8). The VictoriaMetrics stack chart brings the operator, the
database (vmsingle), the scraper (vmagent), the rule evaluator (vmalert),
alertmanager, Grafana, kube-state-metrics and the node exporter.

**It runs on the worker** (`WORKER_AFFINITY`), the node exporter apart, which
runs on every node it reports on. Alertmanager has to: it posts to Home
Assistant at Home Assistant's own address, which only the worker has a route
to (rfc-008 §4.1, operations.md §4). The rest goes with it because the worker
has the memory and the local disk, and the cloud nodes share their cores with
etcd. The database's volume is `local-path` and unbacked: monitoring is
rebuilt fresh rather than migrated (migration.md §3), so losing it costs its
history and nothing else. Grafana is reached by a port-forward until its name
moves (rfc-007 §7.4).

**What the chart would install that this cluster does not** is switched off
(`stack_values`): the sync job, a Helm hook that fetches rules and dashboards
off the upstream projects' default branches at install time, so that what a run
installs would depend on the day it ran; the default rules and dashboards that
job would fetch, which have no playbooks here; the scrape jobs for the
controller manager, the scheduler and etcd, whose metrics Talos does not
expose to a scraper; and the operator's pre-delete cleanup, a second hook.
`helm.v4.Chart` drops hooks, so each is off rather than left to vanish
(cluster-infra.md §1.2).

**Two values the chart generates at render time stay put** across runs: the
operator's webhook certificate and Grafana's admin password. The provider
renders with a server-side dry run, which lets a chart's `lookup` read the
cluster, and both templates reuse the Secret they find there (the operator's
`vm-operator.certs` while `admissionWebhooks.keepTLSSecret` holds, Grafana's
`grafana.password`), so only the first install generates either. A render
with no cluster generates fresh ones every time.

**The alert leg** is alertmanager's one receiver, Home Assistant's webhook for
alertmanager, an automation apart from the one the operations repository posts
to, because alertmanager's body is fixed (rfc-007 §7.3). The webhook's address
is the sealed value `conventions.sealed.ALERT_WEBHOOK`, which the receiver
reads through the operator's `url_secret`, so it is in no rendered
configuration. While it is absent from the stack's configuration, no
SealedSecret is declared for it; the operator then skips the receiver's
configuration, and every alert ends at the chart's `blackhole` receiver until
the value is sealed in (`kluster.lib.k8s.sealed_value`,
physical/bring-up.md §1). The rules are `rules.RULE_GROUPS`, among them the
cluster's check-in, which the receiver repeats on a short interval.

**Scrape objects for Cilium** are declared here rather than in Cilium's
component: they are instances of definitions this chart installs, and Cilium
is installed long before it. The chart declares its own for the kubelet, the
API server and the cluster DNS the same way.

The operator's objects -- the receiver, the rules and the scrape objects --
are declared through `operator_objects`, a layer of its own while the SDK
generated for them cannot be imported.

The namespace is `privileged`, for the node exporter (rfc-007 §8).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components import cilium
from kluster.components.cilium.bgp import CONTROL_PLANE_LABEL
from kluster.components.monitoring import rules
from kluster.components.monitoring.operator_objects import operator_object
from kluster.conventions import backup
from kluster.conventions.alert import Tier
from kluster.lib.k8s import LooserLevel, PodSecurity, helm_chart, namespace, sealed_value
from kluster.lib.versions import ChartPin
from putils import Component

__all__ = (
    'CILIUM_SCRAPES',
    'HEARTBEAT_GROUP_INTERVAL',
    'RECEIVER',
    'WORKER_AFFINITY',
    'CiliumScrape',
    'Monitoring',
    'metrics_server_values',
    'receiver_spec',
    'stack_values',
)

#: What places a pod on the homelab worker: every node that is not a control
#: plane node. The control plane is the cloud nodes (nodes.md §4.2), Talos
#: labels each node of that type with `CONTROL_PLANE_LABEL` and no other, and
#: the worker is the one node `physical` declares with any other role; the BGP
#: session selects its node the same way (`components.cilium.bgp`).
WORKER_AFFINITY: Mapping[str, Any] = {
    'nodeAffinity': {
        'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchExpressions': [{'key': CONTROL_PLANE_LABEL, 'operator': 'DoesNotExist'}]}],
        },
    },
}

#: alertmanager's one receiver, Home Assistant's webhook for alertmanager.
RECEIVER = 'home-assistant'

#: How often alertmanager may send a new notification for the check-in's group,
#: which bounds how late a repeat is: at most one interval after
#: `rules.HEARTBEAT_REPEAT` has passed.
HEARTBEAT_GROUP_INTERVAL = dt.timedelta(minutes=1)


def stack_values() -> dict[str, object]:
    """The VictoriaMetrics stack chart's values: placed on the worker, and the chart's extras off."""
    return {
        # Fetches rules and dashboards at install time, and is a hook.
        'syncJob': {'enabled': False},
        # What the sync job would fetch. Off on their own as well, so that
        # neither arrives should the job ever run.
        'defaultRules': {'enabled': False},
        'defaultDashboards': {'enabled': False},
        # Talos binds the controller manager and the scheduler to 127.0.0.1,
        # and etcd's metrics listener is the bring-up's, read through the API
        # server's node proxy: each job would scrape nothing and alert on its
        # own absence (rfc-007 §7.1).
        'kubeControllerManager': {'enabled': False},
        'kubeScheduler': {'enabled': False},
        'kubeEtcd': {'enabled': False},
        'victoria-metrics-operator': {
            # The stack turns the operator chart's pre-delete hook on; a hook
            # is off rather than left to vanish.
            'crds': {'cleanup': {'enabled': False}},
            # The chart's default, stated: it is what keeps the webhook's
            # certificate from being generated afresh on every run.
            'admissionWebhooks': {'keepTLSSecret': True},
            'affinity': WORKER_AFFINITY,
        },
        'vmsingle': {
            'spec': {
                'affinity': WORKER_AFFINITY,
                # Stated rather than left to the cluster's default class: a
                # claim made before any class is the default would be made with
                # none, and never bind.
                'storage': {'storageClassName': conventions.SC_LOCAL_PATH},
            },
        },
        'vmagent': {'spec': {'affinity': WORKER_AFFINITY}},
        'vmalert': {'spec': {'affinity': WORKER_AFFINITY}},
        'alertmanager': {
            'spec': {
                'affinity': WORKER_AFFINITY,
                # The receiver's configuration is a VMAlertmanagerConfig, whose
                # route the operator otherwise confines to alerts carrying its
                # own namespace's label; alertmanager here has one receiver for
                # every alert, and the check-in carries no namespace at all.
                'disableNamespaceMatcher': True,
            },
        },
        'grafana': {'affinity': WORKER_AFFINITY},
        'kube-state-metrics': {'affinity': WORKER_AFFINITY},
    }


def metrics_server_values() -> dict[str, object]:
    """metrics-server's values: on the worker, and reading each kubelet over the TLS it serves.

    Talos' kubelet serves a self-signed certificate, which no cluster authority
    signs, so metrics-server is told not to verify it; the stack's own kubelet
    scrapes skip the same verification at the chart's defaults. The
    acceptance, its actor and the clause it fails, are recorded in
    item 6 of declarative/cluster-infra.md §1.
    """
    return {'args': ['--kubelet-insecure-tls'], 'affinity': WORKER_AFFINITY}


@dataclass(frozen=True)
class CiliumScrape:
    """One of Cilium's workloads, as vmagent finds its pods, and the container ports its metrics are served on."""

    name: str
    selector: Mapping[str, str]
    ports: tuple[str, ...]


#: Cilium's four metrics endpoints, in the pod labels and port names of the
#: pinned chart (Cilium v1.20.4, the agent's and Envoy's DaemonSets and the
#: operator's Deployment, with the metrics the cilium component enables): the
#: agent's own and Hubble's on the agent, Envoy's, and the operator's. Each is
#: on the host network, at a port the node firewall opens to the cluster's own
#: ranges (`components.talos.POD_REACHED_PORTS`).
CILIUM_SCRAPES: tuple[CiliumScrape, ...] = (
    CiliumScrape('cilium-agent', {'k8s-app': 'cilium'}, ('prometheus', 'hubble-metrics')),
    CiliumScrape('cilium-envoy', {'k8s-app': 'cilium-envoy'}, ('envoy-metrics',)),
    CiliumScrape('cilium-operator', {'io.cilium/app': 'operator'}, ('prometheus',)),
)


class Monitoring(Component, pulumi_type='kluster:monitoring:Monitoring'):
    """The VictoriaMetrics stack and metrics-server, the rules, alertmanager's receiver and Cilium's scrape objects.

    `after` is what the stack is installed behind: Cilium, and the sealing
    controller, whose chart defines the receiver's SealedSecret. It is stated
    on the children rather than left to the component's own `depends_on`,
    which Pulumi does not push down to a component's children.

    `alert_webhook` is the ciphertext the stack's configuration holds for
    `conventions.sealed.ALERT_WEBHOOK`, `None` while it holds none; the `None`
    is the bring-up's tolerance, and goes with it (physical/bring-up.md §1).
    """

    def __init__(
        self,
        name: str,
        *,
        stack_chart: ChartPin,
        metrics_server_chart: ChartPin,
        after: Sequence[pulumi.Resource],
        alert_webhook: Mapping[str, str] | None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        behind = self.child_opts(depends_on=list(after))

        self.namespace = namespace(
            f'{name}-namespace',
            name=conventions.MONITORING_NAMESPACE,
            looser=LooserLevel(
                level=PodSecurity.PRIVILEGED,
                reason="the node exporter reads the host's network, its PID namespace and its paths",
            ),
            opts=behind,
        )
        self.chart = helm_chart(
            name,
            pin=stack_chart,
            namespace=self.namespace.metadata.name,
            values=stack_values(),
            opts=behind,
        )
        self.metrics_server = helm_chart(
            f'{name}-metrics-server',
            pin=metrics_server_chart,
            namespace=self.namespace.metadata.name,
            values=metrics_server_values(),
            opts=behind,
        )

        webhook = conventions.sealed.ALERT_WEBHOOK
        # Behind the sealing controller, whose chart defines the kind, and
        # the namespace the value is sealed to.
        self.alert_webhook = sealed_value(
            f'{name}-webhook',
            webhook,
            ciphertext=alert_webhook,
            opts=self.child_opts(depends_on=[*after, self.namespace]),
        )

        # Instances of the chart's definitions, so behind the chart. Declared
        # through `operator_objects`, while the SDK for them cannot be imported.
        after_the_chart = self.child_opts(depends_on=[self.chart])
        self.receiver = operator_object(
            f'{name}-receiver',
            kind='VMAlertmanagerConfig',
            # The webhook's Secret is read in this object's own namespace.
            metadata=k8s.meta.v1.ObjectMetaArgs(name=RECEIVER, namespace=webhook.namespace),
            spec=receiver_spec(),
            opts=after_the_chart,
        )
        self.rules = operator_object(
            f'{name}-rules',
            kind='VMRule',
            metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
            spec={'groups': [_rule_group(group) for group in rules.RULE_GROUPS]},
            opts=after_the_chart,
        )
        self.cilium_scrapes = [
            operator_object(
                f'{name}-{scrape.name}',
                kind='VMPodScrape',
                metadata=k8s.meta.v1.ObjectMetaArgs(namespace=self.namespace.metadata.name),
                spec={
                    'selector': {'matchLabels': dict(scrape.selector)},
                    'namespaceSelector': {'matchNames': [cilium.NAMESPACE]},
                    'podMetricsEndpoints': [{'port': port} for port in scrape.ports],
                },
                opts=after_the_chart,
            )
            for scrape in CILIUM_SCRAPES
        ]

        self.register_outputs({})


def receiver_spec() -> dict[str, object]:
    """alertmanager's receiver and route: every alert to Home Assistant's webhook, the check-in on a short repeat.

    The webhook's address is read from `conventions.sealed.ALERT_WEBHOOK`'s
    Secret (`url_secret`), so it is in no rendered configuration. Resolved
    alerts are not sent: Home Assistant pushes what fires, and a resolution
    closes nothing on the phone. The check-in is repeated well inside the
    timer it restarts: alertmanager sends a repeat on the first group interval
    after the repeat interval has passed.
    """
    webhook = conventions.sealed.ALERT_WEBHOOK
    (webhook_key,) = webhook.keys
    return {
        'receivers': [
            {
                'name': RECEIVER,
                'webhook_configs': [
                    {'url_secret': {'name': webhook.name, 'key': webhook_key}, 'send_resolved': False},
                ],
            }
        ],
        'route': {
            'receiver': RECEIVER,
            'group_by': ['alertname'],
            'routes': [
                {
                    'receiver': RECEIVER,
                    'matchers': [f'{rules.TIER_LABEL}="{Tier.HEARTBEAT.value}"'],
                    'group_wait': '0s',
                    'group_interval': backup.duration(HEARTBEAT_GROUP_INTERVAL),
                    'repeat_interval': backup.duration(rules.HEARTBEAT_REPEAT),
                }
            ],
        },
    }


def _rule_group(group: rules.RuleGroup) -> dict[str, object]:
    """One rule group as the definition spells it, every rule labeled with its tier and annotated with the rest."""
    return {
        'name': group.name,
        'rules': [
            {
                'alert': rule.alert,
                'expr': rule.expr,
                **({'for': backup.duration(rule.pending)} if rule.pending is not None else {}),
                'labels': {**rule.labels, rules.TIER_LABEL: rule.tier.value},
                'annotations': {'summary': rule.summary, 'playbook': str(rule.playbook)},
            }
            for rule in group.rules
        ],
    }
