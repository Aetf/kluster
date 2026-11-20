"""The cluster's DNS in the `k8s-base` program: one replica on each site, and the Service preferring the one at its client's.

The program runs whole under mocks (`k8s_base_installation`), and the cases
read the two patches it declares over Talos' CoreDNS objects. The
independent source is Talos' manifest itself, transcribed below at v1.13.9:
the patches name its objects and only fields it leaves unset, and the
spread is evaluated with the replica count it sets, against
the cluster's nodes labelled as they are (`node_selection`), by the rule the
scheduler applies to a `DoNotSchedule` spread. Literals written here are
Kubernetes' contracts: the label the EndpointSlice controller reads a node's
zone from, and the label the Deployment controller puts on a replica set's
pods.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence, Set
from typing import Any, cast

import pytest_asyncio
from k8s_base_installation import CHART, Run, applied
from node_selection import NODES, matches

from kluster import conventions

DEPLOYMENT_PATCH = 'kubernetes:apps/v1:DeploymentPatch'
SERVICE_PATCH = 'kubernetes:core/v1:ServicePatch'

#: The label the EndpointSlice controller reads a node's zone from, which a
#: Service's `trafficDistribution` hints are written in (Kubernetes'
#: `v1.LabelTopologyZone`).
ZONE_LABEL = 'topology.kubernetes.io/zone'

#: The label the Deployment controller puts on each replica set's pods.
POD_TEMPLATE_HASH = 'pod-template-hash'

#: The distributions that send a client to an endpoint in its own zone where
#: there is one: `PreferSameZone`, and `PreferClose`, its older name.
SAME_ZONE = ('PreferSameZone', 'PreferClose')

#: One pod at each site.
ONE_PER_SITE = Counter(site.value for site in conventions.Site)

#: Fields of a patch that say which object it patches rather than what it sets.
IDENTITY = {('apiVersion',), ('kind',), ('metadata', 'name'), ('metadata', 'namespace')}

#: Talos' CoreDNS Deployment as its manifest writes it: every field it sets,
#: transcribed from `CoreDNSDeployment` in
#: `internal/app/machined/pkg/controllers/k8s/internal/k8stemplates/coredns.go`
#: at v1.13.9 (commit `3ebd10a7c1bd`), L217–389. The image is the
#: configuration's, so it stands as a placeholder.
TALOS_DEPLOYMENT: dict[str, Any] = {
    'apiVersion': 'apps/v1',
    'kind': 'Deployment',
    'metadata': {
        'name': 'coredns',
        'namespace': 'kube-system',
        'labels': {'k8s-app': 'kube-dns', 'kubernetes.io/name': 'CoreDNS'},
    },
    'spec': {
        'replicas': 2,
        'strategy': {'type': 'RollingUpdate', 'rollingUpdate': {'maxUnavailable': 1}},
        'selector': {'matchLabels': {'k8s-app': 'kube-dns'}},
        'template': {
            'metadata': {'labels': {'k8s-app': 'kube-dns'}},
            'spec': {
                'nodeSelector': {'kubernetes.io/os': 'linux'},
                'affinity': {
                    'podAntiAffinity': {
                        'preferredDuringSchedulingIgnoredDuringExecution': [
                            {
                                'weight': 100,
                                'podAffinityTerm': {
                                    'labelSelector': {
                                        'matchExpressions': [
                                            {'key': 'k8s-app', 'operator': 'In', 'values': ['kube-dns']}
                                        ]
                                    },
                                    'topologyKey': 'kubernetes.io/hostname',
                                },
                            }
                        ]
                    }
                },
                'serviceAccountName': 'coredns',
                'priorityClassName': 'system-cluster-critical',
                'tolerations': [
                    {'key': 'node-role.kubernetes.io/control-plane', 'operator': 'Exists', 'effect': 'NoSchedule'},
                    {
                        'key': 'node.cloudprovider.kubernetes.io/uninitialized',
                        'operator': 'Exists',
                        'effect': 'NoSchedule',
                    },
                ],
                'containers': [
                    {
                        'name': 'coredns',
                        'image': 'the image the configuration names',
                        'imagePullPolicy': 'IfNotPresent',
                        'resources': {'limits': {'memory': '170Mi'}, 'requests': {'cpu': '100m', 'memory': '70Mi'}},
                        'env': [{'name': 'GOMEMLIMIT', 'value': '161MiB'}],
                        'args': ['-conf', '/etc/coredns/Corefile'],
                        'volumeMounts': [{'name': 'config-volume', 'mountPath': '/etc/coredns', 'readOnly': True}],
                        'ports': [
                            {'name': 'dns', 'protocol': 'UDP', 'containerPort': 53},
                            {'name': 'dns-tcp', 'protocol': 'TCP', 'containerPort': 53},
                            {'name': 'metrics', 'protocol': 'TCP', 'containerPort': 9153},
                        ],
                        'livenessProbe': {
                            'httpGet': {'path': '/health', 'port': 8080, 'scheme': 'HTTP'},
                            'initialDelaySeconds': 60,
                            'timeoutSeconds': 5,
                            'successThreshold': 1,
                            'failureThreshold': 5,
                        },
                        'readinessProbe': {'httpGet': {'path': '/ready', 'port': 8181, 'scheme': 'HTTP'}},
                        'securityContext': {
                            'allowPrivilegeEscalation': False,
                            'capabilities': {'add': ['NET_BIND_SERVICE'], 'drop': ['ALL']},
                            'readOnlyRootFilesystem': True,
                        },
                    }
                ],
                'dnsPolicy': 'Default',
                'volumes': [
                    {
                        'name': 'config-volume',
                        'configMap': {'name': 'coredns', 'items': [{'key': 'Corefile', 'path': 'Corefile'}]},
                    }
                ],
            },
        },
    },
}

#: Talos' `kube-dns` Service as its manifest writes it, from `CoreDNSService`
#: in the same file, L23–87, for a cluster with both families: the addresses
#: are the configuration's, so they stand as placeholders.
TALOS_SERVICE: dict[str, Any] = {
    'apiVersion': 'v1',
    'kind': 'Service',
    'metadata': {
        'name': 'kube-dns',
        'namespace': 'kube-system',
        'annotations': {'prometheus.io/scrape': 'true', 'prometheus.io/port': '9153'},
        'labels': {'k8s-app': 'kube-dns', 'kubernetes.io/cluster-service': 'true', 'kubernetes.io/name': 'CoreDNS'},
    },
    'spec': {
        'selector': {'k8s-app': 'kube-dns'},
        'clusterIP': 'the configuration DNS address',
        'ports': [
            {'name': 'dns', 'port': 53, 'protocol': 'UDP', 'targetPort': 53},
            {'name': 'dns-tcp', 'port': 53, 'protocol': 'TCP', 'targetPort': 53},
            {'name': 'metrics', 'port': 9153, 'protocol': 'TCP', 'targetPort': 9153},
        ],
        'clusterIPs': ['the configuration DNS address', 'its IPv6 DNS address'],
        'ipFamilies': ['IPv4', 'IPv6'],
        'ipFamilyPolicy': 'RequireDualStack',
    },
}


@pytest_asyncio.fixture(scope='module', loop_scope='module', name='applied')
async def applied_fixture() -> Run:
    """The stack as its file holds it: every sealed value it declares, sealed in."""
    return await applied()


def _one(run: Run, kind: str) -> dict[str, Any]:
    (declared,) = [it.inputs for it in run.monitor.of_type(kind)]
    return declared


def _spread(run: Run) -> dict[str, Any]:
    (spread,) = _one(run, DEPLOYMENT_PATCH)['spec']['template']['spec']['topologySpreadConstraints']
    return cast('dict[str, Any]', spread)


# -- The scheduler's reading of the spread -----------------------------------


#: A CoreDNS pod: the site it runs at, and its labels.
type Pod = tuple[str, Mapping[str, str]]


def _pod_labels(revision: str) -> dict[str, str]:
    """The labels of a pod of Talos' template, of one replica set."""
    return {**TALOS_DEPLOYMENT['spec']['template']['metadata']['labels'], POD_TEMPLATE_HASH: revision}


def _sites_admitting(
    spread: Mapping[str, Any], incoming: Mapping[str, str], pods: Sequence[Pod], down: Set[str]
) -> set[str]:
    """The sites the scheduler may place a pod labelled `incoming` at, beside `pods`, with the nodes `down` taking none.

    A node's site is its label at the spread's key. A spread that does not
    refuse is a preference, so every site with a node up admits the pod.
    Otherwise a site admits it when, counting the pods the spread selects at
    each site, placing it there leaves that site at most `maxSkew` above the
    least-filled site the spread sees: every node's, under the default
    `nodeTaintsPolicy`, and only the nodes up under `Honor`. The pods counted
    are those its selector matches and that agree with `incoming` on each of
    its `matchLabelKeys`. That is kube-scheduler's rule at v1.36.3: the filter
    in `pkg/scheduler/framework/plugins/podtopologyspread/filtering.go`
    L338–353, and the default taints policy in `common.go` L112–114. A spread
    with a field the model does not read, such as `minDomains` or
    `nodeAffinityPolicy`, is refused rather than read as if it were absent.
    """
    assert set(spread) <= {
        'topologyKey',
        'maxSkew',
        'whenUnsatisfiable',
        'labelSelector',
        'matchLabelKeys',
        'nodeTaintsPolicy',
    }, spread
    key = spread['topologyKey']
    honor_taints = spread.get('nodeTaintsPolicy', 'Ignore') == 'Honor'
    up = {labels[key] for node, labels in NODES.items() if key in labels and node not in down}
    if spread['whenUnsatisfiable'] != 'DoNotSchedule':
        return up
    seen = up if honor_taints else {labels[key] for labels in NODES.values() if key in labels}
    same = cast('list[str]', spread.get('matchLabelKeys', []))
    counted = Counter(
        site
        for site, labels in pods
        if matches(spread['labelSelector'], labels) and all(labels.get(it) == incoming.get(it) for it in same)
    )
    least = min(counted[site] for site in seen)
    return {site for site in up if counted[site] + 1 - least <= spread['maxSkew']}


def _rollouts(spread: Mapping[str, Any], old: Sequence[Pod], replicas: int) -> Iterator[Counter[str]]:
    """Where a new replica set's pods end, over every order the old pods can go in, for each rollout that finishes.

    Before each new pod is placed, any of the old pods still running may
    already be gone; once every new pod is placed, the old ones go.
    """

    def placing(remaining: Sequence[Pod], placed: list[Pod]) -> Iterator[Counter[str]]:
        if len(placed) == replicas:
            yield Counter(site for site, _ in placed)
            return
        incoming = _pod_labels('new')
        for kept in itertools.chain.from_iterable(
            itertools.combinations(remaining, size) for size in range(len(remaining) + 1)
        ):
            for site in _sites_admitting(spread, incoming, [*kept, *placed], set()):
                yield from placing(list(kept), [*placed, (site, incoming)])

    return placing(old, [])


# -- The spread ----------------------------------------------------------------


def test_the_spread_is_over_the_site_every_node_carries(applied: Run) -> None:
    """Its key is the site label, so its domains are the sites, each of which some node is at."""
    key = _spread(applied)['topologyKey']

    assert {labels.get(key) for labels in NODES.values()} == set(conventions.Site)


def test_the_spread_counts_the_pods_of_talos_template(applied: Run) -> None:
    """A selector that matched no CoreDNS pod would count none, and the spread would place every replica anywhere."""
    assert matches(_spread(applied)['labelSelector'], _pod_labels('any'))


def test_the_manifests_replicas_land_one_on_each_site(applied: Run) -> None:
    """Placed one after the other, in any order the scheduler may take, Talos' replicas end one at each site."""
    spread = _spread(applied)
    replicas = TALOS_DEPLOYMENT['spec']['replicas']

    ends = list(_rollouts(spread, [], replicas))

    assert ends
    assert all(it == ONE_PER_SITE for it in ends), ends


def test_a_rollout_ends_with_one_new_replica_on_each_site(applied: Run) -> None:
    """Whichever old pod the rollout removes first, the new replica set ends one pod at each site.

    The old pods, one at each site, still run while the new ones are placed,
    until the rollout removes them; a spread that counted them could leave
    both new pods in the cloud once the old home pod goes.
    """
    spread = _spread(applied)
    replicas = TALOS_DEPLOYMENT['spec']['replicas']
    old = [(site, _pod_labels('old')) for site in conventions.Site]

    ends = list(_rollouts(spread, old, replicas))

    assert ends
    assert all(it == ONE_PER_SITE for it in ends), ends


def test_with_the_home_node_down_its_replica_waits_rather_than_joining_the_cloud_one(applied: Run) -> None:
    """The replica the down worker held is placed nowhere: two at the cloud site would leave home without one.

    The cloud replica keeps running, since a spread decides only where a new
    pod goes; the home replica is placed again when the worker takes pods.
    """
    spread = _spread(applied)
    home = {node for node, labels in NODES.items() if labels[conventions.SITE_LABEL] == conventions.Site.HOME}
    running = [(conventions.Site.CLOUD.value, _pod_labels('current'))]

    assert home
    assert _sites_admitting(spread, _pod_labels('current'), running, home) == set()
    assert _sites_admitting(spread, _pod_labels('current'), running, set()) == {conventions.Site.HOME.value}


# -- The Service -----------------------------------------------------------------


def test_the_service_prefers_the_replica_at_its_clients_site(applied: Run) -> None:
    """Its traffic goes to an endpoint in the client's zone, and a node's zone is its site."""
    service = _one(applied, SERVICE_PATCH)

    assert service['spec']['trafficDistribution'] in SAME_ZONE
    assert conventions.SITE_LABEL == ZONE_LABEL


# -- The layer over Talos' objects -------------------------------------------------


def _paths(value: object, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every field a manifest sets, as the keys leading to it, the items of a list under one `[]`."""
    if prefix:
        yield prefix
    if isinstance(value, Mapping):
        for key, it in cast('Mapping[str, object]', value).items():
            yield from _paths(it, (*prefix, key))
    elif isinstance(value, list):
        for it in cast('list[object]', value):
            yield from _paths(it, (*prefix, '[]'))


def _leaves(value: object, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every value a manifest sets, as the keys leading to it."""
    if isinstance(value, Mapping):
        for key, it in cast('Mapping[str, object]', value).items():
            yield from _leaves(it, (*prefix, key))
    elif isinstance(value, list):
        for it in cast('list[object]', value):
            yield from _leaves(it, (*prefix, '[]'))
    else:
        yield prefix


def test_each_patch_names_the_object_talos_manifest_declares(applied: Run) -> None:
    """A patch of an object that does not exist fails; one of another object patches the wrong thing."""
    for kind, manifest in ((DEPLOYMENT_PATCH, TALOS_DEPLOYMENT), (SERVICE_PATCH, TALOS_SERVICE)):
        patch = _one(applied, kind)
        for path in IDENTITY:
            assert _at(patch, path) == _at(manifest, path), (kind, path)


def test_the_patches_set_only_fields_talos_manifest_leaves_unset(applied: Run) -> None:
    """Talos' apply of its manifest then never names a field the patches own, and so never takes one.

    A field the manifest sets, a list it writes or a map key it writes, is
    one Talos' forced apply would take back on its next sync.
    """
    for kind, manifest in ((DEPLOYMENT_PATCH, TALOS_DEPLOYMENT), (SERVICE_PATCH, TALOS_SERVICE)):
        set_by_talos = set(_paths(manifest))
        patched = set(_leaves(_one(applied, kind))) - IDENTITY

        assert patched, kind
        assert sorted(patched & set_by_talos) == [], kind


def test_the_patches_wait_for_the_cni(applied: Run) -> None:
    """No pod of the rollout the Deployment's patch waits for becomes ready before Cilium is installed."""
    cilium = applied.urn(CHART, 'cilium')
    for kind in (DEPLOYMENT_PATCH, SERVICE_PATCH):
        (patch,) = applied.monitor.of_type(kind)

        assert cilium in _behind(applied, applied.urn(kind, patch.name)), kind


def _at(value: object, path: tuple[str, ...]) -> object:
    for key in path:
        value = cast('dict[str, object]', value).get(key) if isinstance(value, dict) else None
    return value


def _behind(run: Run, urn: str) -> set[str]:
    """Every registration `urn` waits for, through the children of each component it waits for and their dependencies."""
    registrations = run.monitor.registrations
    reached: set[str] = set()
    frontier = list(registrations[urn].dependencies)
    while frontier:
        dependency = frontier.pop()
        if dependency in reached:
            continue
        reached.add(dependency)
        frontier.extend(registrations[dependency].dependencies if dependency in registrations else [])
        frontier.extend(other for other, request in registrations.items() if request.parent == dependency)
    return reached
