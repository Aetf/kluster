"""The `k8s-base` program's stand-ins and its whole run: what `physical` has published, the pins, the fetched files.

Every suite that runs `k8s_base.main` whole needs the same three things the
program reads besides its kubeconfig, so they are here rather than in any one
of those suites (framework/testing.md §2): `test_stack_programs.py` runs it for
its providers, `test_cilium.py` for what Cilium declares, and every suite that
takes an `applied` fixture for what the other components install. `applied`
is that run, and `Run` reads what it registered. `applied(sealed=SEALED)` is
the same run with every sealed value the stack declares sealed in, which the
stack holds only once the bring-up has sealed them (physical/bring-up.md §1).
`sealed_values` builds those values for any stack, and `test_stack_programs.py`
seals in each of its programs' values through it.

Every value is invented. The addresses are documentation and private ranges
of the families each output carries, and each output holds addresses no other
output holds, so a pool built from the wrong output reads differently.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

import pulumi
import pytest
from client_kubeconfig import KUBECONFIG
from mock_monitor import Recorder, declaring, run_under_backstop

from kluster import conventions
from kluster.conventions.outputs import BOOT_VOLUME
from kluster.lib.k8s import KUBECONFIG_KEY
from kluster.lib.stack_addresses import UnusableAddressOutput
from kluster.lib.versions import ManifestPin, SourcePin

OUTPUTS = conventions.PHYSICAL_OUTPUTS

#: What a fully applied `physical` publishes under each output this program
#: could read, the ones it must not included: the reserved public address is
#: a public IPv4 that is no pool member, and the kubeconfig is a secret that
#: never crosses a StackReference.
PUBLISHED: dict[str, object] = {
    OUTPUTS.cluster_endpoint: '203.0.113.10',
    OUTPUTS.cluster_endpoint_v6: '2001:db8::10',
    OUTPUTS.vip1: '203.0.113.20',
    OUTPUTS.vip1_private: '10.0.1.99',
    OUTPUTS.node_private_ips: {'cp-1': '10.0.1.11', 'cp-2': '10.0.1.12', 'cp-3': '10.0.1.13'},
    OUTPUTS.node_guas: {'cp-1': '2001:db8:1::11', 'cp-2': '2001:db8:1::12', 'cp-3': '2001:db8:1::13'},
    OUTPUTS.worker_gua: '2001:db8:70::10',
    OUTPUTS.volume_ids: {
        node: {
            BOOT_VOLUME: f'ocid1.bootvolume.oc1.phx.{node}',
            **{
                name: f'ocid1.volume.oc1.phx.{name}'
                for name, entry in conventions.NODE_VOLUMES.items()
                if entry.attached_node == node
            },
        }
        for node in conventions.CLOUD_NODES
    },
    OUTPUTS.kubeconfig: {},
}

DIGEST = f'sha256:{"d" * 64}'

#: The pins the program reads, as the engine hands them to a stack program: a
#: project-level object as the JSON text of its `value:`, a scalar as itself
#: (framework/pulumi.md §3.2). Each chart and image added beside Cilium's is
#: at a version and a digest no real pin names, and one chart is served from
#: an HTTP repository, so one installed from anything but its pin reads
#: differently.
VERSIONS_CONFIG = {
    'versions:chart-cilium': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '1.20.1',
            'digest': DIGEST,
            'definitions': False,
        }
    ),
    'versions:chart-sealed-secrets': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.1.0',
            'digest': f'sha256:{"1" * 64}',
            'definitions': True,
        }
    ),
    'versions:chart-cert-manager': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': 'v9.2.0',
            'digest': f'sha256:{"2" * 64}',
            'definitions': True,
            'render-values': {'crds.enabled': 'true'},
        }
    ),
    'versions:chart-reloader': json.dumps(
        {
            'repository': 'https://charts.example.invalid/',
            'version': '9.3.0',
            'definitions': False,
        }
    ),
    'versions:chart-cloudnative-pg': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.6.0',
            'digest': f'sha256:{"5" * 64}',
            'definitions': True,
            'floor': {'operator': '1.26', 'document': 'a stand-in floor'},
        }
    ),
    'versions:chart-plugin-barman-cloud': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.7.0',
            'digest': f'sha256:{"6" * 64}',
            'definitions': True,
        }
    ),
    'versions:chart-volsync': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.8.0',
            'digest': f'sha256:{"7" * 64}',
            'definitions': True,
        }
    ),
    'versions:chart-node-feature-discovery': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.9.0',
            'digest': f'sha256:{"8" * 64}',
            'definitions': True,
        }
    ),
    'versions:chart-intel-device-plugins-operator': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.10.0',
            'digest': f'sha256:{"9" * 64}',
            'definitions': True,
        }
    ),
    'versions:chart-intel-device-plugins-gpu': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.11.0',
            'digest': f'sha256:{"a" * 64}',
            'definitions': False,
        }
    ),
    'versions:chart-victoria-metrics-k8s-stack': json.dumps(
        {
            'repository': 'oci://registry.example.invalid/charts',
            'version': '9.12.0',
            'digest': f'sha256:{"b" * 64}',
            'definitions': True,
        }
    ),
    'versions:chart-metrics-server': json.dumps(
        {
            'repository': 'https://charts.example.invalid/',
            'version': '9.13.0',
            'definitions': False,
        }
    ),
    'versions:image-local-path-provisioner': f'registry.example.invalid/local-path-provisioner:v9.4.0@sha256:{"3" * 64}',
    'versions:image-local-path-helper': f'registry.example.invalid/busybox:9.5.0@sha256:{"4" * 64}',
    'versions:image-metrics-history': f'registry.example.invalid/victoria-metrics:v9.15.0@sha256:{"0" * 64}',
    'versions:image-metrics-backup': f'registry.example.invalid/vmbackup:v9.15.0@sha256:{"1" * 64}',
    'versions:image-metrics-restore': f'registry.example.invalid/vmrestore:v9.15.0@sha256:{"2" * 64}',
    'versions:image-metrics-bucket': f'registry.example.invalid/curl:9.16.0@sha256:{"3" * 64}',
    'versions:image-oci-sdk': f'registry.example.invalid/oci-sdk:9.17.0-{"a" * 12}@sha256:{"4" * 64}',
    'versions:image-etcd-backup': f'registry.example.invalid/talos-backup:v9.18.0@sha256:{"5" * 64}',
    'versions:manifest-gateway-api': json.dumps(
        {
            'repository': 'example/gateway-api',
            'release': 'v1.6.1',
            'asset': 'experimental-install.yaml',
            'sha256': 'e' * 64,
        }
    ),
    'versions:source-external-snapshotter': json.dumps(
        {
            'repository': 'example/external-snapshotter',
            'tag': 'v9.14.0',
            'commit': 'f' * 40,
            'files': {'crd/classes.yaml': 'c' * 64, 'crd/snapshots.yaml': '5' * 64},
        }
    ),
}

#: The release asset's bytes, as a fetch that matched the pin returns them.
#: Nothing parses them under mocks; the case asks that these, and nothing
#: else, reach the declaration.
GATEWAY_API_DEFINITIONS = '# the Gateway API definitions the pin names\n'


def source_file(pin: SourcePin, path: str) -> str:
    """A file of a source pin, as a fetch that matched the pin returns it: text naming where it came from.

    Nothing parses it under mocks; it names the repository, the commit and the
    path, so text that reached a declaration says which pin's file it is.
    """
    return f'# {pin.repository}@{pin.commit}:{path}\n'


class Physical(Recorder):
    """A `physical` whose StackReference hands out `published`."""

    def __init__(self, published: dict[str, object] | None = None) -> None:
        super().__init__()
        self.published = PUBLISHED if published is None else published

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': self.published}
        return {}


@contextmanager
def release_assets() -> Generator[list[ManifestPin]]:
    """The program's fetches answered here, and every release-asset pin it was asked for recorded.

    The program fetches the release asset and the source files before it
    declares anything, so a run that reaches the network is one the suite does
    not control. A source pin's files are answered by `source_file`.
    """
    from kluster.stacks import k8s_base

    fetched: list[ManifestPin] = []

    def fetch(pin: ManifestPin) -> str:
        fetched.append(pin)
        return GATEWAY_API_DEFINITIONS

    def fetch_source(pin: SourcePin) -> list[str]:
        return [source_file(pin, path) for path in pin.files]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(k8s_base, 'fetch_manifest', fetch)
        patch.setattr(k8s_base, 'fetch_source_files', fetch_source)
        yield fetched


#: The type the provider registers a chart under.
CHART = 'kubernetes:helm.sh/v4:Chart'

#: The suffix `Autonaming` gives a Kubernetes object the program names none
#: for, as the provider's random one would be: `<logical name>-<suffix>`.
AUTONAME_SUFFIX = '0a1b2c3d'


class Autonaming(Physical):
    """A `physical` whose Kubernetes objects are named the way the provider names one given none.

    The provider fills in `metadata.name` from the logical name and a random
    suffix when the program states none; the mock echoes inputs only, so an
    autonamed object would read back nameless and a reference to it as `None`.
    """

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        outputs = super().computed(args)
        metadata = cast('dict[str, Any]', args.inputs).get('metadata')
        if args.typ.startswith('kubernetes:') and metadata is not None and 'name' not in metadata:
            outputs['metadata'] = metadata | {'name': f'{args.name}-{AUTONAME_SUFFIX}'}
        return outputs


class Run:
    """One run of the program: what it registered, the pins it fetched, and the refusal that stopped it, if one did."""

    def __init__(
        self, monitor: Recorder, fetched: list[ManifestPin], refused: UnusableAddressOutput | None = None
    ) -> None:
        self.monitor = monitor
        self.fetched = fetched
        self.refused = refused

    def inputs(self, typ: str, name: str) -> dict[str, Any]:
        return self.monitor.inputs_of(name, typ)

    def values(self, chart: str) -> dict[str, Any]:
        return self.inputs(CHART, chart)['values']

    def urn(self, typ: str, name: str) -> str:
        """The URN of the one registration of this type under this name.

        More than one is refused rather than resolved to whichever registered
        first: independent registrations are recorded in the order they reach the
        monitor on the SDK's executor threads, so a first-found answer is a
        different resource on a different run (`Recorder.one` refuses the same
        way).
        """
        found = [
            urn for urn, request in self.monitor.registrations.items() if (request.type, request.name) == (typ, name)
        ]
        assert len(found) == 1, f'{len(found)} registrations of {typ} answer to {name}: {found}'
        return found[0]

    def dependencies(self, typ: str, name: str) -> list[str]:
        return list(self.monitor.registrations[self.urn(typ, name)].dependencies)

    def children(self, component: str) -> list[tuple[str, str, list[str]]]:
        """Each resource registered under the one component of type `component`: its type, name and dependencies."""
        (parent,) = [urn for urn, request in self.monitor.registrations.items() if request.type == component]
        return [
            (request.type, request.name, list(request.dependencies))
            for request in self.monitor.registrations.values()
            if request.parent == parent
        ]


def sealed_values(stack: str) -> dict[str, object]:
    """A ciphertext for every key of every sealed value the census has `stack` declare.

    That is what the stack's configuration holds once each is sealed in. It is
    built from the census, so a row added to it is sealed in here too.
    """
    return {
        value.name: {key: f'AgB-sealed-{value.name}-{key}' for key in value.keys}
        for value in conventions.sealed.VALUES.values()
        if value.stack == stack
    }


#: Every sealed value `k8s-base` declares, sealed in (`sealed_values`).
SEALED: dict[str, object] = sealed_values(conventions.STACK_NAMES.k8s_base)


async def declare(physical: Physical, *, preview: bool = False, sealed: dict[str, object] | None = None) -> Run:
    """The whole program against `physical`, a refusal caught and every registration settled.

    `sealed` is what the stack holds under its sealed key, which is nothing
    when it is `None`.
    """
    from kluster.stacks import k8s_base

    config = {f'kluster:{KUBECONFIG_KEY}': KUBECONFIG}
    if sealed is not None:
        config[f'kluster:{conventions.sealed.CONFIG_KEY}'] = json.dumps(sealed)
    pulumi.runtime.set_all_config(config | VERSIONS_CONFIG, secret_keys=[f'kluster:{KUBECONFIG_KEY}'])
    monitor = await run_under_backstop(physical, stack=conventions.STACK_NAMES.k8s_base, preview=preview)
    refused = None
    with release_assets() as fetched:
        try:
            async with declaring():
                await k8s_base.main()
        except UnusableAddressOutput as error:
            refused = error
    return Run(monitor, fetched, refused)


async def applied(*, sealed: dict[str, object] | None = None) -> Run:
    """The whole program against a `physical` that has published every output, which no refusal stops.

    A suite's `applied` fixture is this, once per module. The fixture itself
    stays in the suite: one imported by name would be shadowed by every case
    that takes it as an argument, which the linter reports as a redefinition.
    """
    run = await declare(Autonaming(), sealed=sealed)
    assert run.refused is None, f'the program refused a physical that published every output: {run.refused}'
    return run
