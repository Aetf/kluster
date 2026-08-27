# Pulumi's mock monitor and its gRPC message types carry no type information,
# and this file reaches inside them to read the resource options Pulumi
# records. The unknown-type family is suppressed here rather than repo-wide.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""The worker VM under libvirt, and the domain that was there before it.

Two very different risks share this host. The worker is ordinary
infrastructure — it can be rebuilt — but the Home Assistant domain beside it
cannot: it carries the home's automation, it predates this program, and a diff
that proposed replacing it would be an outage nobody asked for. So the
assertions here are mostly about what the program must *never* do — recreate
the adopted domain, resize the worker's disk out from under it — and those are
resource options, invisible in any later `pulumi diff` that goes well.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, cast

import pulumi
import pulumi.runtime.mocks
import pytest
import pytest_asyncio

# The mock monitor keeps no record of the options a resource was registered
# with — `import`, `ignoreChanges` and `deleteBeforeReplace` are exactly what
# this module is about, and none of them is reachable from an output. They are
# recovered here, before any Pulumi code runs (framework/testing.md §3.1).
REQUESTS: dict[str, Any] = {}

_original_register_resource = pulumi.runtime.mocks.MockMonitor.RegisterResource


def _patched_register_resource(self: Any, request: Any) -> Any:
    REQUESTS[request.name] = request
    return _original_register_resource(self, request)


pulumi.runtime.mocks.MockMonitor.RegisterResource = _patched_register_resource

CLUSTER = 'kluster'
WORKER = 'worker'
DOMAIN = f'{CLUSTER}-{WORKER}'
HAOS_UUID = '5e10948c-8934-4239-849c-b6b9104bfe3f'
MACHINE_CONFIG = 'machine: {}\n'
STORAGE_DIR = '/var/lib/libvirt/kluster'
BRIDGE = 'kvmbr1'
URI = 'qemu+ssh://virt@192.0.2.7/system'
VCPUS = 12
MEMORY_GIB = 10

#: Where the Talos `nocloud` image has been decompressed for the provider to
#: upload. What matters below is that the volume is created *from* it.
IMAGE_PATH = '/var/tmp/kluster-talos-images/talos-v1.11.0-nocloud-amd64-abc123.raw'

#: A domain as the libvirt provider builds one before it defines it: a virtio
#: data disk and the seed as a cdrom. The stylesheet under test is applied to
#: this shape, so what is asserted is the XML libvirt would receive.
PROVIDER_DOMAIN_XML = """<domain type='kvm'>
  <name>kluster-worker</name>
  <devices>
    <disk type='volume' device='disk'>
      <driver name='qemu' type='raw'/>
      <source volume='kluster-worker.raw'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='kluster-worker-seed.iso'/>
      <target dev='hda' bus='ide'/>
    </disk>
  </devices>
</domain>
"""


class Fake(pulumi.runtime.Mocks):
    """Enough of the Talos and libvirt providers to declare the host.

    The Talos half answers with a secrets bundle and a rendered machine
    configuration, because the seed's whole job is to carry that
    configuration; the libvirt half echoes its inputs, which is what a
    definition-only provider does.
    """

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str | None, dict[str, Any]]:
        outputs: dict[str, Any] = dict(cast('dict[str, Any]', args.inputs))
        if args.typ == 'talos:machine/secrets:Secrets':
            outputs['machineSecrets'] = {
                'certs': {},
                'cluster': {'id': CLUSTER},
                'secrets': {'secretboxEncryptionSecret': 'c2VjcmV0Ym94'},
            }
            outputs['clientConfiguration'] = {'caCertificate': 'ca', 'clientCertificate': 'crt', 'clientKey': 'key'}
        return args.name + '_id', outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == 'talos:machine/getConfiguration:getConfiguration':
            return {'machineConfiguration': MACHINE_CONFIG}, []
        return {}, []


@pytest_asyncio.fixture(autouse=True)
async def setup_mocks() -> None:
    REQUESTS.clear()
    pulumi.runtime.set_mocks(Fake(), project='kluster', stack='physical', preview=False)


def build_cluster(worker_nodes: tuple[str, ...] = (WORKER,)) -> Any:
    from kluster.physical.talos import TalosCluster

    return TalosCluster(
        CLUSTER,
        cluster_name=CLUSTER,
        endpoint='https://203.0.113.10:6443',
        cert_sans=['203.0.113.10'],
        control_plane_nodes=('cp1',),
        worker_nodes=worker_nodes,
        talos_version='v1.11.0',
    )


def build(**kwargs: Any) -> Any:
    from kluster.physical.homelab import HomelabHost

    kwargs.setdefault('cluster', build_cluster())
    kwargs.setdefault('connection_uri', URI)
    kwargs.setdefault('storage_dir', STORAGE_DIR)
    kwargs.setdefault('bridge', BRIDGE)
    kwargs.setdefault('vcpus', VCPUS)
    kwargs.setdefault('memory_gib', MEMORY_GIB)
    kwargs.setdefault('image_path', IMAGE_PATH)
    kwargs.setdefault('haos_domain_uuid', HAOS_UUID)
    return HomelabHost(CLUSTER, **kwargs)


# -- the adopted domain -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_home_assistant_domain_is_adopted_by_uuid() -> None:
    request = await registration(build().haos)

    # Without an import id this declaration would *create* a second Home
    # Assistant domain on the host. With it, a stack that has never run
    # previews an import — which is what makes the program valid before the
    # adoption has happened.
    assert request.importId == HAOS_UUID
    assert request.type == 'libvirt:index/domain:Domain'


@pytest.mark.asyncio
async def test_the_adopted_domain_states_nothing_about_itself() -> None:
    request = await registration(build().haos)

    # The domain's disks and passthrough devices are its identity and they are
    # the host's to describe. An input here is a claim about a machine this
    # program did not build, and on import a claim that differs from the truth
    # is applied *to the domain*.
    assert dict(request.object) == {}


@pytest.mark.asyncio
async def test_no_attribute_of_the_adopted_domain_is_the_programs_to_change() -> None:
    import pulumi_libvirt as libvirt

    from kluster.physical.homelab import HOST_OWNED

    ignored = set((await registration(build().haos)).ignoreChanges)

    # The ratchet: a provider release that adds a domain attribute lands here
    # as a failure, rather than as a silent proposal to change something about
    # a running Home Assistant.
    parameters = [
        name for name in inspect.signature(libvirt.DomainArgs.__init__).parameters if name not in ('__self__', 'self')
    ]
    assert parameters
    assert {_camel(name) for name in parameters} <= ignored
    assert ignored == set(HOST_OWNED)


@pytest.mark.asyncio
async def test_the_adopted_domain_cannot_be_replaced() -> None:
    host = build()

    # The last line of defence. If the import diff is wrong in a way nobody
    # foresaw, protection turns "replace this domain" into a refused
    # operation instead of a home with no automation.
    assert host.haos._protect is True  # pyright: ignore[reportPrivateUsage]


def test_a_uuid_reaches_the_domain_whatever_case_it_was_typed_in() -> None:
    from kluster.physical.homelab import import_id

    # The id Pulumi records has to be the one the provider reads back.
    assert import_id(HAOS_UUID.upper()) == HAOS_UUID


def test_a_domain_name_is_not_a_domain_uuid() -> None:
    from kluster.physical.homelab import import_id

    with pytest.raises(ValueError, match='not a domain UUID'):
        import_id('haos')


def test_an_import_id_that_is_not_known_yet_is_refused() -> None:
    from kluster.physical.homelab import import_id

    # An import id is a resource option, not an input: it is read while the
    # program is being constructed, so an output would arrive too late and
    # silently become "create a second domain".
    with pytest.raises(ValueError, match='known before the run'):
        import_id(pulumi.Output.from_input(HAOS_UUID))


# -- the worker VM ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_disk_is_a_raw_image_in_its_own_pool() -> None:
    host = build()

    assert await host.volume.format.future() == 'raw'
    # Not the host's existing image pool: this one points at the nodatacow
    # subvolume, which is what makes a VM image on btrfs behave.
    assert await host.volume.pool.future() == await host.pool.name.future()
    target = await host.pool.target.future()
    assert target is not None
    assert target.path == STORAGE_DIR


@pytest.mark.asyncio
async def test_the_worker_boots_because_the_volume_is_created_from_the_talos_image() -> None:
    host = build()

    # The difference between a declared VM and a bootable one. The provider
    # reads this path where the program runs and uploads the bytes into the
    # pool; without it the domain comes up on an empty disk.
    assert await host.volume.source.future() == IMAGE_PATH


@pytest.mark.asyncio
async def test_the_disk_is_not_sized_by_the_declaration() -> None:
    host = build()
    request = await registration(host.volume)

    # The provider refuses `size` beside `source` outright — it takes the
    # volume's capacity from the image — so stating the worker's intended disk
    # size here would not shrink-wrap anything, it would fail the apply.
    assert 'size' not in dict(request.object)
    assert await host.volume.size.future() is None


@pytest.mark.asyncio
async def test_growing_the_disk_is_a_host_operation_not_a_diff() -> None:
    host = build()
    request = await registration(host.volume)

    # The volume is created at the image's size and grown on the host with
    # `truncate` plus `virsh blockresize`, so the file and the state part
    # company from the first day. Every field of a libvirt volume replaces the
    # volume, so a refresh that read the grown file back and diffed against it
    # would propose destroying the worker's disk.
    assert 'size' in set(request.ignoreChanges)
    assert host.volume._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_talos_upgrade_is_not_a_proposal_to_rewrite_the_disk() -> None:
    request = await registration(build().volume)

    # `source` says what the disk was written with, and Talos upgrades itself
    # in place over its machine API — so the declaration stops describing the
    # volume as soon as the node is upgraded. Left diffable, the next
    # `talosVersion` bump would propose replacing a running node's boot disk,
    # and `protect` would then refuse every apply until the bump was reverted.
    assert 'source' in set(request.ignoreChanges)


@pytest.mark.asyncio
async def test_the_seed_carries_the_worker_configuration_and_nothing_generated() -> None:
    host = build()

    assert await host.seed.user_data.future() == MACHINE_CONFIG
    metadata = json.loads(str(await host.seed.meta_data.future()))
    # A fresh instance-id on every apply would tell a nocloud datasource that
    # a long-running machine is booting for the first time.
    assert metadata == {'instance-id': DOMAIN, 'local-hostname': DOMAIN}


@pytest.mark.asyncio
async def test_the_worker_is_sized_in_the_units_libvirt_uses() -> None:
    host = build()

    assert await host.domain.vcpu.future() == VCPUS
    # MiB, not GiB: the same number in the wrong unit is a 10 MiB node.
    assert await host.domain.memory.future() == MEMORY_GIB * 1024


@pytest.mark.asyncio
async def test_the_worker_joins_the_second_bridge() -> None:
    host = build()
    interfaces = await host.domain.network_interfaces.future()
    assert interfaces is not None

    interface = interfaces[0]
    assert interface.bridge == BRIDGE
    # A real bridge, not macvtap: host-to-guest traffic is load-bearing here
    # (the NAS role serves NFS into the cluster), and macvtap is exactly the
    # path that cannot hairpin back to the host.
    assert interface.macvtap is None
    assert interface.network_name is None


@pytest.mark.asyncio
async def test_the_worker_comes_back_from_a_host_reboot() -> None:
    host = build()

    assert await host.domain.autostart.future() is True
    assert await host.domain.running.future() is True


@pytest.mark.asyncio
async def test_a_resized_worker_is_undefined_before_it_is_redefined() -> None:
    request = await registration(build().domain)

    # `vcpu` and `memory` replace the domain rather than update it, and the
    # domain's name is stated rather than generated — so a replacement that
    # created first would collide with the domain it is replacing.
    assert request.deleteBeforeReplace is True
    assert request.deleteBeforeReplaceDefined is True


@pytest.mark.asyncio
async def test_both_domains_are_reached_through_one_connection() -> None:
    host = build()
    worker, haos = await registration(host.domain), await registration(host.haos)

    # Two definitions on one host, one credential: the adopted domain is not
    # reached through the ambient default provider.
    assert worker.provider
    assert haos.provider == worker.provider


@pytest.mark.asyncio
async def test_a_second_worker_is_a_design_change_not_a_default() -> None:
    with pytest.raises(ValueError, match='exactly one worker VM'):
        build(cluster=build_cluster(worker_nodes=(WORKER, 'worker2')))


# -- the disk tuning the provider cannot express ----------------------------


def test_the_data_disk_returns_freed_space_to_the_host() -> None:
    from kluster.physical.homelab import disk_tuning_xslt

    disk = _transformed_disk("disk[@device='disk']")

    # `discard=unmap` is what makes in-guest TRIM punch holes back out of the
    # sparse image; without it the file only ever grows, and the migration
    # interleaves reclamation with growth on a disk that has no slack.
    assert disk.attrib['discard'] == 'unmap'
    # `cache=none` keeps guest I/O out of the host page cache, which would
    # otherwise cache it a second time.
    assert disk.attrib['cache'] == 'none'
    assert disk.attrib['type'] == 'raw'
    assert 'raw' in disk_tuning_xslt()


def test_the_seed_is_left_exactly_as_the_provider_wrote_it() -> None:
    # The stylesheet is a scalpel: the cdrom wants neither setting, and a
    # transform that reached it would be changing a disk it does not
    # understand.
    cdrom = _transformed_disk("disk[@device='cdrom']")

    assert 'discard' not in cdrom.attrib
    assert 'cache' not in cdrom.attrib


async def registration(resource: pulumi.Resource) -> Any:
    """The request a resource was registered with, once it has been.

    Registration is a background task, so a component's constructor returning
    is not the moment its resources reached the monitor; resolving the URN is.
    """
    return REQUESTS[str(await resource.urn.future()).rsplit('::', 1)[-1]]


def _transformed_disk(selector: str) -> Any:
    """One disk's driver element, after the domain XML has been through XSLT."""
    from kluster.physical.homelab import disk_tuning_xslt

    etree = pytest.importorskip('lxml.etree', reason='lxml is present as a dependency of pykeepass')
    transform = etree.XSLT(etree.XML(disk_tuning_xslt().encode()))
    result = transform(etree.XML(PROVIDER_DOMAIN_XML.encode()))
    driver = result.getroot().find(f'devices/{selector}/driver')
    assert driver is not None
    return driver


def _camel(name: str) -> str:
    return re.sub(r'_(.)', lambda match: match.group(1).upper(), name)
