"""The physical program's installation stand-in, and the run that points the program at it.

Every suite that runs `physical.main` whole reads the same invented account
and appliance answers and the same stack configuration, so they are here
rather than in any one of those suites (framework/testing.md §2).
`test_physical_stack.py` holds the stack's cases against them and
`test_physical_links.py` its link invariant.

`install` is what each of those suites' own autouse fixture awaits: the
fixture stays in the suite, where the cases that take it are.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pulumi
import pytest
from mock_monitor import run_under_backstop
from oci_conventions import with_compartment, with_tenancy_ocid
from unifi_controller import Controller

from kluster import conventions
from kluster.lib import workstation

LB_ADDRESS = '203.0.113.10'
LB_ADDRESS_V6 = '2001:db8::10'
VIP1_ADDRESS = '203.0.113.20'
VNIC_ID = 'ocid1.vnic.oc1.phx.vip'
AVAILABILITY_DOMAIN = 'ZRbp:PHX-AD-1'
OBJECT_NAMESPACE = 'axmpletenancy'
TENANCY_ID = 'ocid1.tenancy.oc1..test'
BUDGET_RECIPIENTS = ['alerts@example.invalid', 'second@example.invalid']
ZT_NETWORK_ID = '0123456789abcdef'
#: The worker VM's global address, as the operator reads it off the cluster
#: VLAN's router advertisement. Nothing derives it — that is the point of the
#: key — so the value only has to be inside a documentation prefix.
WORKER_GUA = '2001:db8:1:70::10'
KUBECONFIG = 'apiVersion: v1\nkind: Config\n'
TALOSCONFIG = 'context: kluster\n'
#: The libvirt client identity, as it arrives from configuration. Nothing
#: parses it here — the run writes it to a file and hands the provider that
#: path — so its shape only has to be something no test could mistake for real.
LIBVIRT_KEY = '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n-----END OPENSSH PRIVATE KEY-----\n'

#: A manifest digest, as a root filesystem pin carries one. Nothing here checks
#: the bytes behind it; the shape is what the reader is checked against.
DIGEST = f'sha256:{"f" * 64}'
#: The tag the pins below name. Invented, like the digest: what matters is that
#: the convention turns the pair into the reference a push pulls by.
ROOTFS_TAG = '7'

#: What the gateway reads out of stack configuration: two secrets a file's
#: content is rendered from, the controller's key, and one measurement. Every
#: value here is invented; what the test is for is that the keys line up and the
#: values reach the right resource.
GATEWAY_CONFIG = {
    'kluster:gatewayPrivateKey': '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n',
    'kluster:gatewayBgpPassword': 'a-session-password',
    'kluster:gatewayAcmeToken': 'a-zone-scoped-token',
    'kluster:unifiApiKey': 'a-controller-key',
    'kluster:workerGua': WORKER_GUA,
    'kluster:zerotierApiToken': 'a-central-token',
}

#: The version pins a stack program reads, in the namespace they share
#: (framework/pulumi.md §3.2). They are project-level configuration in the
#: committed tree — one copy for five stacks — and the runtime cannot tell that
#: from a stack's own key, which is exactly why one namespace works.
VERSIONS_CONFIG = {
    'versions:talos': 'v1.11.0',
    **{
        f'versions:image-{conventions.gateway.image_pin(service)}': (
            f'{conventions.gateway.image_repository(service.artifact)}:{ROOTFS_TAG}@{DIGEST}'
        )
        for service in conventions.gateway.SERVICES
    },
}

#: What the two accounts' providers are built from. Every value is invented;
#: what the suite is for is that each is read at the line that builds its
#: provider and that everything below that line inherits the result.
ACCOUNT_CONFIG = {
    'kluster:ociUserOcid': 'ocid1.user.oc1..test',
    'kluster:ociFingerprint': ':'.join(['ab'] * 16),
    'kluster:ociPrivateKey': '-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----',
    'kluster:b2ApplicationKeyId': 'a-b2-key-id',
    'kluster:b2ApplicationKey': 'a-b2-key',
}


class Installation(Controller):
    """Every account and appliance the program reaches, as far as it reads them back.

    The values are invented; what the suite is for is that each is read at the
    right line and reaches the right resource. The controller's zone lookup is
    the shared stand-in's (`unifi_controller`), on the site the gateway is
    declared against.
    """

    def __init__(self) -> None:
        super().__init__(site=conventions.gateway.UNIFI_SITE)
        #: The arguments each machine configuration was rendered from. An
        #: invoke's arguments survive nowhere else, and two of them are read
        #: back: the cluster endpoint, the one place the Kubernetes API port is
        #: written as a URL, and the patches carrying the firewall's openings.
        self.configurations: list[dict[str, Any]] = []

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        match args.typ:
            case 'oci:Core/vcn:Vcn':
                return {'ipv6cidrBlocks': ['2603:c020:8000:1200::/56']}
            case 'oci:NetworkLoadBalancer/networkLoadBalancer:NetworkLoadBalancer':
                return {
                    'ipAddresses': [
                        {'ipAddress': LB_ADDRESS, 'isPublic': True, 'ipVersion': 'IPV4'},
                        {'ipAddress': LB_ADDRESS_V6, 'isPublic': True, 'ipVersion': 'IPV6'},
                    ]
                }
            case 'talos:machine/secrets:Secrets':
                return {'machineSecrets': {'cluster': {'id': 'test'}}}
            case 'talos:cluster/kubeconfig:Kubeconfig':
                return {'kubeconfigRaw': KUBECONFIG}
            case 'zerotier:index/identity:Identity':
                # Keyed by the resource name, so a test can tell one member's key
                # material from another's: which identity an export carries is the
                # whole of the contract the credential map reads it under.
                return {'identityId': f'{args.name}-node', 'publicKey': 'public', 'privateKey': f'{args.name}-secret'}
            case 'zerotier:index/network:Network':
                return {'networkId': ZT_NETWORK_ID}
            case 'oci:Core/instance:Instance':
                return {'availabilityDomain': AVAILABILITY_DOMAIN}
            case 'oci:Core/publicIp:PublicIp':
                return {'ipAddress': VIP1_ADDRESS}
            case 'b2:index/bucket:Bucket':
                return {'bucketId': 'b2-bucket-id'}
            case 'b2:index/applicationKey:ApplicationKey':
                return {'applicationKeyId': args.name + '-key-id', 'applicationKey': args.name + '-secret'}
            case _:
                return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        match args.token:
            case 'oci:Core/getServices:getServices':
                return {'services': [{'id': 'ocid1.service.os', 'name': 'Object Storage', 'cidrBlock': 'oci-os'}]}
            case 'oci:Core/getVnicAttachments:getVnicAttachments':
                return {'vnicAttachments': [{'vnicId': VNIC_ID}]}
            case 'oci:Core/getVnic:getVnic':
                return {'ipv6addresses': ['2603:c020:8000:1200::a']}
            case 'oci:Identity/getAvailabilityDomains:getAvailabilityDomains':
                return {'availabilityDomains': [{'name': 'ZRbp:PHX-AD-1'}]}
            case 'oci:Identity/getFaultDomains:getFaultDomains':
                return {'faultDomains': [{'name': f'FAULT-DOMAIN-{n}'} for n in (1, 2, 3)]}
            case 'talos:machine/getConfiguration:getConfiguration':
                self.configurations.append(dict(cast('dict[str, Any]', args.args)))
                return {'machineConfiguration': 'machine: {}'}
            case 'talos:client/getConfiguration:getConfiguration':
                return {'talosConfig': TALOSCONFIG}
            case 'talos:cluster/getHealth:getHealth':
                return {'id': 'healthy'}
            case 'oci:ObjectStorage/getNamespace:getNamespace':
                return {'namespace': OBJECT_NAMESPACE}
            case 'talos:imageFactory/getUrls:getUrls':
                # Two artifacts of the same family: the cloud nodes' OCI image
                # and the worker's `nocloud` disk image, which the factory
                # serves compressed.
                platform = str(cast('dict[str, Any]', args.args)['platform'])
                suffix = 'raw.xz' if platform == 'nocloud' else 'qcow2'
                url = f'https://factory.talos.dev/image/test/v1.11.0/{platform}-arch.{suffix}'
                return {'urls': {'diskImage': url}}
            case _:
                return super().answer(args)


#: The compartment the stack acts in, as `conventions` will carry it once the
#: mint has made it. It is patched in rather than configured because that is
#: what the program reads: a compartment is a boundary this repository decides,
#: so it is code and not a config key (credentials.md §3).
COMPARTMENT = conventions.Compartment(
    consumer=conventions.PHYSICAL,
    name=f'{conventions.CLUSTER_NAME}-{conventions.PHYSICAL}',
    ocid='ocid1.compartment.test',
)


#: The whole of what the stack reads out of configuration: the version pins,
#: the secrets that configure the two accounts' providers, and the handful of
#: values an operator supplies or a booted machine reports. Nothing here names
#: a provider namespace — with every provider explicit there is nothing left to
#: configure through one (rfc-002 §8.1).
STACK_CONFIG = {
    'kluster:budgetAlertRecipients': json.dumps(BUDGET_RECIPIENTS),
    # The §3 area, the homelab: the credential the host is reached with, and
    # nothing else.
    # There is no endpoint among them — it is derived — and no storage
    # directory either: the host's own configuration management has to name the
    # same one, which makes it a convention.
    'kluster:libvirtPrivateKey': LIBVIRT_KEY,
    **VERSIONS_CONFIG,
    **ACCOUNT_CONFIG,
    **GATEWAY_CONFIG,
}


async def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Installation:
    """Configure the physical program and start a run against a fresh installation."""
    with_compartment(monkeypatch, COMPARTMENT)
    with_tenancy_ocid(monkeypatch, TENANCY_ID)
    # The run materializes the libvirt session's credential into the checkout's
    # `.credentials/`, so every run is pointed at a directory of its own:
    # a test suite that wrote into the tree it runs from would leave a key
    # behind and, worse, overwrite the operator's.
    monkeypatch.setattr(workstation, 'repo_root', lambda: tmp_path)
    pulumi.runtime.set_all_config(dict(STACK_CONFIG))
    return await run_under_backstop(Installation(), stack='physical')
