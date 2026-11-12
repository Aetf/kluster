"""Monitoring, declared against mocks: the stack's settings, its placement, the rules and alertmanager's receiver.

The program runs whole, against a `physical` that has published its addresses
(`k8s_base_installation`), twice: as it stands before alertmanager's webhook is
sealed in, and with it sealed in (rfc-007 §7, physical/bring-up.md §1).

Literals written here are upstream contracts: the charts' value names, the
label Talos puts on a control plane node, Kubernetes' node-affinity and Pod
Security semantics, and how the VictoriaMetrics operator and alertmanager read
a receiver's route. What the program derives from a convention, a pin or a
census is held to that.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pulumi
import pytest
import pytest_asyncio
import yaml
from fences import prose
from k8s_base_installation import SEALED, VERSIONS_CONFIG, Autonaming, Run, applied, release_assets
from mock_monitor import declaring, run_under_backstop
from section_numbers import sections

from kluster import conventions
from kluster.components.monitoring import RECEIVER
from kluster.components.monitoring.operator_objects import type_token
from kluster.components.monitoring.rules import HEARTBEAT, HEARTBEAT_TIMER
from kluster.conventions.alert import Tier
from kluster.lib.k8s import KUBECONFIG_KEY
from kluster.lib.versions import versions

ROOT = Path(__file__).resolve().parents[1]

CHART = 'kubernetes:helm.sh/v4:Chart'
NAMESPACE = 'kubernetes:core/v1:Namespace'
SEALED_SECRET = 'crds:bitnami.com/v1alpha1:SealedSecret'
VM_RULE = type_token('VMRule')
VM_ALERTMANAGER_CONFIG = type_token('VMAlertmanagerConfig')
VM_POD_SCRAPE = type_token('VMPodScrape')

#: The logical names of the component's two charts.
STACK = 'monitoring'
METRICS_SERVER = 'monitoring-metrics-server'

WEBHOOK = conventions.sealed.ALERT_WEBHOOK

#: The label Talos puts on every control plane node and on no other
#: (`node_label_spec.go` L83-85 at Talos v1.13.9).
TALOS_CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'
#: The label the kubelet gives every node, its own name.
HOSTNAME_LABEL = 'kubernetes.io/hostname'

#: A playbook as the payload carries it: a document from the repository's root,
#: and a section (`conventions.alert.FIELDS`).
PLAYBOOK = re.compile(r'^(?P<document>\S+\.md) §(?P<section>\d+(?:\.\d+)*)$')


async def declare(*, sealed: dict[str, object] | None) -> tuple[Run, list[str]]:
    """The whole program with `sealed` as its sealed values, and every warning it logged."""
    from kluster.stacks import k8s_base

    config = {f'kluster:{KUBECONFIG_KEY}': 'a-fake-kubeconfig-that-reaches-no-cluster'}
    if sealed is not None:
        config[f'kluster:{conventions.sealed.CONFIG_KEY}'] = json.dumps(sealed)
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=[f'kluster:{KUBECONFIG_KEY}'])
    monitor = await run_under_backstop(Autonaming(), stack=conventions.STACK_NAMES.k8s_base)
    warned: list[str] = []

    def warn(message: str, *_: object, **__: object) -> None:
        warned.append(message)

    with release_assets() as fetched, pytest.MonkeyPatch.context() as patch:
        patch.setattr(pulumi.log, 'warn', warn)
        async with declaring():
            await k8s_base.main()
    return Run(monitor, fetched), warned


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def absent() -> tuple[Run, list[str]]:
    """The stack as it stands before the webhook is sealed in."""
    return await declare(sealed=None)


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def sealed() -> Run:
    """Every sealed value the stack declares sealed in, the webhook among them."""
    return await applied(sealed=SEALED)


def _values(run: Run) -> dict[str, Any]:
    return run.values(STACK)


def _declared_rules(run: Run) -> list[dict[str, Any]]:
    return [
        rule
        for declared in run.monitor.of_type(VM_RULE)
        for group in declared.inputs['spec']['groups']
        for rule in group['rules']
    ]


def _receiver(run: Run) -> dict[str, Any]:
    (declared,) = run.monitor.of_type(VM_ALERTMANAGER_CONFIG)
    return declared.inputs


# -- The installation -------------------------------------------------------------


@pytest.mark.parametrize('chart', [STACK, METRICS_SERVER])
def test_each_chart_is_installed_from_its_pin(absent: tuple[Run, list[str]], chart: str) -> None:
    """The reference and the version are the pin's; an HTTP repository's address is passed beside the name."""
    run, _ = absent
    pin = versions.chart[{STACK: 'victoria-metrics-k8s-stack', METRICS_SERVER: 'metrics-server'}[chart]]
    inputs = run.inputs(CHART, chart)

    assert (inputs['chart'], inputs['version']) == (pin.reference, pin.version)
    assert inputs.get('repositoryOpts', {}).get('repo') == (None if pin.oci else pin.repository)


@pytest.mark.parametrize(
    'path',
    [
        ('syncJob', 'enabled'),
        ('defaultRules', 'enabled'),
        ('defaultDashboards', 'enabled'),
        ('kubeControllerManager', 'enabled'),
        ('kubeScheduler', 'enabled'),
        ('kubeEtcd', 'enabled'),
        ('victoria-metrics-operator', 'crds', 'cleanup', 'enabled'),
    ],
    ids='.'.join,
)
def test_what_the_chart_would_fetch_hook_or_scrape_for_nothing_is_off(
    absent: tuple[Run, list[str]], path: tuple[str, ...]
) -> None:
    """The sync job and the operator's cleanup are hooks, the defaults are downloads, and the three jobs scrape nothing.

    Each is on at the chart's own values (rfc-007 §7.1, cluster-infra.md §1.2),
    so each has to be stated off.
    """
    run, _ = absent
    value: Any = _values(run)
    for key in path:
        value = value[key]

    assert value is False


def test_the_namespace_is_privileged_for_the_node_exporter(absent: tuple[Run, list[str]]) -> None:
    """Talos enforces `baseline`, which refuses the node exporter's host network, PID namespace and paths."""
    run, _ = absent
    namespaces = run.monitor.by_name(NAMESPACE)

    (labels,) = [
        ns['metadata']['labels']
        for ns in namespaces.values()
        if ns['metadata']['name'] == conventions.MONITORING_NAMESPACE
    ]
    assert labels['pod-security.kubernetes.io/enforce'] == 'privileged'
    assert run.inputs(CHART, STACK)['namespace'] == conventions.MONITORING_NAMESPACE


def _admits(node: Mapping[str, str], selector: Mapping[str, str], affinity: Mapping[str, Any]) -> bool:
    """Whether a pod with this `nodeSelector` and `affinity` may be scheduled on a node with these labels.

    Kubernetes' rule for the required part: every `nodeSelector` pair matches,
    and at least one `nodeSelectorTerm` has every expression hold.
    """
    if any(node.get(key) != value for key, value in selector.items()):
        return False
    required = affinity.get('nodeAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution')
    if required is None:
        return True

    def holds(expression: Mapping[str, Any]) -> bool:
        key, operator, values = expression['key'], expression['operator'], expression.get('values', [])
        return {
            'In': lambda: node.get(key) in values,
            'NotIn': lambda: node.get(key) not in values,
            'Exists': lambda: key in node,
            'DoesNotExist': lambda: key not in node,
        }[operator]()

    return any(all(holds(e) for e in term.get('matchExpressions', [])) for term in required['nodeSelectorTerms'])


def test_alertmanager_can_schedule_on_the_worker_alone(absent: tuple[Run, list[str]]) -> None:
    """Alertmanager posts to Home Assistant at an address only the worker reaches (rfc-008 §4.1).

    The nodes are labeled as Talos labels them: every cloud node is a control
    plane node, and the worker is not.
    """
    run, _ = absent
    spec = _values(run)['alertmanager']['spec']
    nodes = {
        **{node: {HOSTNAME_LABEL: node, TALOS_CONTROL_PLANE_LABEL: ''} for node in conventions.CLOUD_NODES},
        conventions.HOMELAB_NODE: {HOSTNAME_LABEL: conventions.HOMELAB_NODE},
    }

    schedulable = {
        name
        for name, labels in nodes.items()
        if _admits(labels, spec.get('nodeSelector', {}), spec.get('affinity', {}))
    }

    assert schedulable == {conventions.HOMELAB_NODE}


# -- The rules ---------------------------------------------------------------------


def test_every_declared_rule_carries_a_census_tier_a_summary_and_a_playbook_that_resolves(
    absent: tuple[Run, list[str]],
) -> None:
    """The playbook rule as a gate: an alert whose response procedure is not written down is not shipped.

    Every rule the program declares, whatever built it, is read off the
    declaration: its tier is one `conventions.alert.Tier` defines, its summary
    is a line, and its playbook names a document of this repository and a
    section that document has (architecture.md §4.3, operations.md §5).
    """
    run, _ = absent
    declared = _declared_rules(run)
    tiers = {tier.value for tier in Tier}

    assert declared, 'the program declared no rule'
    for rule in declared:
        assert rule['labels'].get('tier') in tiers, rule['alert']
        summary = rule['annotations'].get('summary', '')
        assert summary.strip() and '\n' not in summary, rule['alert']
        playbook = PLAYBOOK.match(rule['annotations'].get('playbook', ''))
        assert playbook, f'{rule["alert"]}: {rule["annotations"].get("playbook")!r} names no document and section'
        document = ROOT / playbook['document']
        assert document.is_file(), f'{rule["alert"]}: no {playbook["document"]}'
        assert playbook['section'] in sections(prose(document.read_text())), (
            f'{rule["alert"]}: {playbook["document"]} has no §{playbook["section"]}'
        )


def test_the_rules_are_declared_behind_the_chart_that_defines_them(absent: tuple[Run, list[str]]) -> None:
    """pulumi-kubernetes waits for workloads, not for a definition's registration, so the order is declared."""
    run, _ = absent
    chart = run.urn(CHART, STACK)
    objects = [
        (typ, declared.name)
        for typ in (VM_RULE, VM_ALERTMANAGER_CONFIG, VM_POD_SCRAPE)
        for declared in run.monitor.of_type(typ)
    ]

    assert objects
    for typ, name in objects:
        assert chart in run.dependencies(typ, name), name


# -- alertmanager's receiver ----------------------------------------------------------


def test_the_receiver_reads_the_webhooks_address_from_its_sealed_secret(absent: tuple[Run, list[str]]) -> None:
    """The address is in no rendered configuration: every webhook names the sealed value's Secret, in its namespace.

    The operator reads `url_secret` from the receiver object's own namespace
    (`vmalertmanagerconfig_types.go` L533-563 at operator v0.74.0), which is
    therefore the namespace the value is sealed to.
    """
    run, _ = absent
    receiver = _receiver(run)
    webhooks = [config for spec in receiver['spec']['receivers'] for config in spec.get('webhook_configs', [])]

    assert receiver['metadata']['namespace'] == WEBHOOK.namespace
    assert webhooks
    for webhook in webhooks:
        assert 'url' not in webhook
        assert webhook['url_secret']['name'] == WEBHOOK.name
        assert webhook['url_secret']['key'] in WEBHOOK.keys


def test_the_receiver_sends_firing_alerts_alone(absent: tuple[Run, list[str]]) -> None:
    """Home Assistant's automation pushes every alert it is sent, reading no status (operations.md §4).

    A resolution sent there would push as a second alarm. Alertmanager sends
    resolutions to a webhook unless told not to, so the field is stated on
    every webhook rather than left to that default.
    """
    run, _ = absent
    webhooks = [config for spec in _receiver(run)['spec']['receivers'] for config in spec.get('webhook_configs', [])]

    assert webhooks
    for webhook in webhooks:
        assert webhook.get('send_resolved') is False


def test_the_sealed_webhook_is_declared_where_the_receiver_reads_it(sealed: Run) -> None:
    """Sealed in, the value becomes the Secret the receiver names, at the scope the census row seals it."""
    receiver = _receiver(sealed)
    (secret,) = [
        it.inputs for it in sealed.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WEBHOOK.name
    ]

    assert secret['metadata']['namespace'] == receiver['metadata']['namespace']
    assert secret['spec']['encryptedData'] == SEALED[WEBHOOK.name]
    assert secret['metadata']['annotations'] == {f'sealedsecrets.bitnami.com/{WEBHOOK.scope.value}': 'true'}


def test_an_absent_webhook_declares_no_sealed_secret_and_warns_naming_it(absent: tuple[Run, list[str]]) -> None:
    """Until it is sealed in, nothing is declared for it; the receiver is declared all the same, and stays pending."""
    run, warned = absent

    assert [it for it in run.monitor.of_type(SEALED_SECRET) if it.inputs['metadata']['name'] == WEBHOOK.name] == []
    assert [message for message in warned if repr(WEBHOOK.name) in message]
    assert run.monitor.of_type(VM_ALERTMANAGER_CONFIG)


_DURATION = re.compile(r'^(\d+)([smhdw])$')
_UNITS = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days', 'w': 'weeks'}


def _duration(text: str) -> dt.timedelta:
    match = _DURATION.match(text)
    assert match, f'{text!r} is no single-unit duration'
    return dt.timedelta(**{_UNITS[match[2]]: int(match[1])})


def _route(route: Mapping[str, Any], labels: Mapping[str, str]) -> Mapping[str, Any]:
    """The route an alert with these labels ends on, below one that already matched it.

    alertmanager's walk: the first child whose matchers all hold, recursively;
    a child with `continue` passes the alert on to its later siblings too,
    which none here sets. Each matcher is an equality, the one form written
    here. A child inherits what it does not set.
    """
    for child in route.get('routes', []):
        if all(_matches(matcher, labels) for matcher in child.get('matchers', [])):
            return _route({**{k: v for k, v in route.items() if k not in ('routes', 'matchers')}, **child}, labels)
    return route


def _matches(matcher: str, labels: Mapping[str, str]) -> bool:
    name, value = re.fullmatch(r'\s*([\w]+)\s*=\s*"(.*)"\s*', matcher).groups()  # pyright: ignore[reportOptionalMemberAccess] -- an unparsable matcher fails the case
    return labels.get(name) == value


def _top_route(run: Run) -> Mapping[str, Any]:
    """The receiver's route as the operator places it under alertmanager's root.

    The operator adds `namespace = "<the object's namespace>"` to the top route
    of every VMAlertmanagerConfig unless the VMAlertmanager disables it
    (`config.go` `buildRoute` at operator v0.74.0).
    """
    receiver = _receiver(run)
    route = dict(receiver['spec']['route'])
    if not _values(run)['alertmanager']['spec'].get('disableNamespaceMatcher', False):
        route['matchers'] = [*route.get('matchers', []), f'namespace="{receiver["metadata"]["namespace"]}"']
    return route


def test_every_rule_reaches_home_assistant_and_the_check_in_repeats_inside_its_timer(
    absent: tuple[Run, list[str]],
) -> None:
    """Each declared rule's alert, carrying its own labels alone, ends on Home Assistant's receiver.

    The check-in carries no label but its tier, so a route confined to a
    namespace would drop it. Its repeat reaches Home Assistant at most a group
    interval after the repeat interval, which is inside the timer it restarts
    (rfc-007 §7.3).
    """
    run, _ = absent
    top = _top_route(run)
    declared = _declared_rules(run)

    for rule in declared:
        labels = cast('Mapping[str, str]', rule['labels'])
        top_matches = all(_matches(matcher, labels) for matcher in top.get('matchers', []))
        assert top_matches, f'{rule["alert"]} does not reach the receiver'
        assert _route(top, labels)['receiver'] == RECEIVER, rule['alert']

    (check_in,) = [rule for rule in declared if rule['alert'] == HEARTBEAT.alert]
    route = _route(top, check_in['labels'])
    latest = _duration(route['repeat_interval']) + _duration(route['group_interval'])
    assert latest < HEARTBEAT_TIMER


# -- The workaround layer: its tripwire, and what it gives up --------------------------


def test_the_operators_generated_sdk_still_cannot_be_imported() -> None:
    """While it cannot, `components.monitoring.operator_objects` declares the operator's kinds untyped.

    The day this fails, the SDK imports (Aetf/kluster-ops#720): each call
    there becomes the SDK class of its kind, and that module goes.
    """
    with pytest.raises(SyntaxError, match='duplicate argument'):
        importlib.import_module('pulumi_crds.operator.v1beta1')


#: The definitions the program's custom resources are generated from, as
#: `update_crds` bundled them from the pinned charts.
BUNDLE = ROOT / 'packages' / 'crds' / 'crds.yaml'


def _spec_schemas() -> dict[tuple[str, str], Mapping[str, Any]]:
    """Each bundled definition's `spec` schema, by `apiVersion` and kind."""
    schemas: dict[tuple[str, str], Mapping[str, Any]] = {}
    for loaded in yaml.safe_load_all(BUNDLE.read_text()):
        document = cast('dict[str, Any] | None', loaded)
        if not isinstance(document, dict) or document.get('kind') != 'CustomResourceDefinition':
            continue
        body = document['spec']
        for version in body['versions']:
            spec = version['schema']['openAPIV3Schema']['properties'].get('spec')
            if spec is not None:
                schemas[(f'{body["group"]}/{version["name"]}', body['names']['kind'])] = spec
    return schemas


def _unknown_fields(value: object, schema: Mapping[str, Any], path: str) -> list[str]:
    """Every path in `value` its schema does not declare, where the schema fixes its keys.

    An object the schema leaves open -- `x-kubernetes-preserve-unknown-fields`
    or `additionalProperties` -- takes any key, as the API server does; a
    field outside a closed object is pruned by the API server on apply,
    silently, which is the failure this walk exists to catch.
    """
    if isinstance(value, Mapping):
        mapping = cast('Mapping[str, object]', value)
        properties = cast('Mapping[str, Any]', schema.get('properties', {}))
        open_object = schema.get('x-kubernetes-preserve-unknown-fields') is True or schema.get(
            'additionalProperties'
        ) not in (None, False)
        found: list[str] = []
        for key, item in mapping.items():
            if key in properties:
                found += _unknown_fields(item, properties[key], f'{path}.{key}')
            elif not open_object:
                found.append(f'{path}.{key}')
        return found
    if isinstance(value, list):
        items = cast('Mapping[str, Any]', schema.get('items', {}))
        return [
            field
            for i, item in enumerate(cast('list[object]', value))
            for field in _unknown_fields(item, items, f'{path}[{i}]')
        ]
    return []


@pytest.fixture(scope='module', name='spec_schemas')
def spec_schemas_fixture() -> dict[tuple[str, str], Mapping[str, Any]]:
    return _spec_schemas()


def test_every_untyped_operator_object_is_spelled_as_its_definition_declares(
    absent: tuple[Run, list[str]], spec_schemas: dict[tuple[str, str], Mapping[str, Any]]
) -> None:
    """The check the SDK's typed `Args` would make, while the layer declares the operator's kinds untyped.

    It goes with `components.monitoring.operator_objects` (Aetf/kluster-ops#720).
    """
    run, _ = absent
    declared = [it for typ in (VM_RULE, VM_ALERTMANAGER_CONFIG, VM_POD_SCRAPE) for it in run.monitor.of_type(typ)]

    assert declared
    for it in declared:
        schema = spec_schemas[(it.inputs['apiVersion'], it.inputs['kind'])]
        assert _unknown_fields(it.inputs['spec'], schema, 'spec') == [], it.name


def test_the_spelling_check_names_a_field_its_definition_lacks(
    spec_schemas: dict[tuple[str, str], Mapping[str, Any]],
) -> None:
    """Its control: a misspelled field below a closed object is named; one below an open object is not."""
    schema = spec_schemas[('operator.victoriametrics.com/v1beta1', 'VMAlertmanagerConfig')]
    spec = {
        'receivers': [{'name': 'r', 'webhook_configs': [{'send_resolvd': False}]}],
        'route': {'receiver': 'r', 'routes': [{'anything': 'goes'}]},
    }

    assert _unknown_fields(spec, schema, 'spec') == ['spec.receivers[0].webhook_configs[0].send_resolvd']
