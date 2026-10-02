"""The `k8s-base` program's stand-ins: what `physical` has published, the pins, and the release asset.

Every suite that runs `k8s_base.main` whole needs the same three things the
program reads besides its kubeconfig, so they are here rather than in any one
of those suites (framework/testing.md §2): `test_stack_programs.py` runs it for
its providers, and `test_cilium.py` and `test_standing_set.py` for what it
installs.

Every value is invented. The addresses are documentation and private ranges
of the families each output carries, and each output holds addresses no other
output holds, so a pool built from the wrong output reads differently.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pulumi
import pytest
from mock_monitor import Recorder

from kluster import conventions
from kluster.lib.versions import ManifestPin

OUTPUTS = conventions.PHYSICAL_OUTPUTS

#: What a fully applied `physical` publishes under each output this program
#: could read, the ones it must not included: the nodes' public IPv4s and the
#: reserved public address are public IPv4s that are no pool member, and the
#: kubeconfig is a secret that never crosses a StackReference.
PUBLISHED: dict[str, object] = {
    OUTPUTS.cluster_endpoint: '203.0.113.10',
    OUTPUTS.cluster_endpoint_v6: '2001:db8::10',
    OUTPUTS.vip1: '203.0.113.20',
    OUTPUTS.vip1_private: '10.0.1.99',
    OUTPUTS.node_private_ips: {'cp-1': '10.0.1.11', 'cp-2': '10.0.1.12', 'cp-3': '10.0.1.13'},
    OUTPUTS.node_public_ips: {'cp-1': '198.51.100.11', 'cp-2': '198.51.100.12', 'cp-3': '198.51.100.13'},
    OUTPUTS.node_guas: {'cp-1': '2001:db8:1::11', 'cp-2': '2001:db8:1::12', 'cp-3': '2001:db8:1::13'},
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
    'versions:image-local-path-provisioner': f'registry.example.invalid/local-path-provisioner:v9.4.0@sha256:{"3" * 64}',
    'versions:image-local-path-helper': f'registry.example.invalid/busybox:9.5.0@sha256:{"4" * 64}',
    'versions:manifest-gateway-api': json.dumps(
        {
            'repository': 'example/gateway-api',
            'release': 'v1.6.1',
            'asset': 'experimental-install.yaml',
            'sha256': 'e' * 64,
        }
    ),
}

#: The release asset's bytes, as a fetch that matched the pin returns them.
#: Nothing parses them under mocks; the case asks that these, and nothing
#: else, reach the declaration.
GATEWAY_API_DEFINITIONS = '# the Gateway API definitions the pin names\n'


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
    """The program's release-asset fetch answered here, and every pin it was asked for recorded.

    The program fetches the asset before it declares anything, so a run that
    reaches the network is one the suite does not control.
    """
    from kluster.stacks import k8s_base

    fetched: list[ManifestPin] = []

    def fetch(pin: ManifestPin) -> str:
        fetched.append(pin)
        return GATEWAY_API_DEFINITIONS

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(k8s_base, 'fetch_manifest', fetch)
        yield fetched
