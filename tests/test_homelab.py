"""The worker VM under libvirt, and the session that declares it.

The worker is ordinary infrastructure — it can be rebuilt — but the disk
under it is not, so the assertions here are mostly about what the program must
*never* do: resize or rewrite that disk out from under a running node. Those
are resource options, invisible in any later `pulumi diff` that goes well.

The home-automation domain on the same host is declared nowhere, so nothing
here asserts about it (rfc-002 §13).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions

CLUSTER = 'kluster'
WORKER = 'worker'
DOMAIN = f'{CLUSTER}-{WORKER}'
MACHINE_CONFIG = 'machine: {}\n'
STORAGE_DIR = '/var/lib/libvirt/kluster'
BRIDGE = 'kvmbr1'
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


class Providers(Recorder):
    """Enough of the Talos and libvirt providers to declare the host.

    The Talos half answers with a secrets bundle and a rendered machine
    configuration, because the seed's whole job is to carry that
    configuration; the libvirt half echoes its inputs, which is what a
    definition-only provider does.
    """

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ != 'talos:machine/secrets:Secrets':
            return {}
        return {
            'machineSecrets': {
                'certs': {},
                'cluster': {'id': CLUSTER},
                'secrets': {'secretboxEncryptionSecret': 'c2VjcmV0Ym94'},
            },
            'clientConfiguration': {'caCertificate': 'ca', 'clientCertificate': 'crt', 'clientKey': 'key'},
        }

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'talos:machine/getConfiguration:getConfiguration':
            return {'machineConfiguration': MACHINE_CONFIG}
        return {}


@pytest_asyncio.fixture(autouse=True)
async def monitor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Providers:
    from kluster.components.homelab import PRIVATE_KEY
    from kluster.lib import workstation

    # The component materializes the session's credential into the checkout's
    # `.credentials/`, so every case here is pointed at a checkout of its own:
    # a suite that wrote into the tree it runs from would leave a key behind
    # and, worse, overwrite the operator's.
    monkeypatch.setattr(workstation, 'repo_root', lambda: tmp_path)
    pulumi.runtime.set_all_config({f'kluster:{PRIVATE_KEY}': IDENTITY})
    return await run_with(Providers(), stack='physical')


def build_cluster(worker_nodes: tuple[str, ...] = (WORKER,)) -> Any:
    from kluster.components.talos import TalosCluster

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
    from kluster.components.homelab import HomelabHost

    kwargs.setdefault('cluster', build_cluster())
    kwargs.setdefault('host', str(conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address))
    kwargs.setdefault('storage_dir', STORAGE_DIR)
    kwargs.setdefault('bridge', BRIDGE)
    kwargs.setdefault('vcpus', VCPUS)
    kwargs.setdefault('memory_gib', MEMORY_GIB)
    kwargs.setdefault('image_path', IMAGE_PATH)
    return HomelabHost(CLUSTER, **kwargs)


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
async def test_the_disk_is_not_sized_by_the_declaration(monitor: Providers) -> None:
    host = build()
    request = await registration(monitor, host.volume)

    # The provider refuses `size` beside `source` outright — it takes the
    # volume's capacity from the image — so stating the worker's intended disk
    # size here would not shrink-wrap anything, it would fail the apply.
    assert 'size' not in dict(request.object)
    assert await host.volume.size.future() is None


@pytest.mark.asyncio
async def test_growing_the_disk_is_a_host_operation_not_a_diff(monitor: Providers) -> None:
    host = build()
    request = await registration(monitor, host.volume)

    # The volume is created at the image's size and grown on the host with
    # `truncate` plus `virsh blockresize`, so the file and the state part
    # company from the first day. Every field of a libvirt volume replaces the
    # volume, so a refresh that read the grown file back and diffed against it
    # would propose destroying the worker's disk.
    assert 'size' in set(request.ignoreChanges)
    assert host.volume._protect is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_a_talos_upgrade_is_not_a_proposal_to_rewrite_the_disk(monitor: Providers) -> None:
    request = await registration(monitor, build().volume)

    # `source` says what the disk was written with, and Talos upgrades itself
    # in place over its machine API — so the declaration stops describing the
    # volume as soon as the node is upgraded. Left diffable, the next
    # `versions:talos` bump would propose replacing a running node's boot disk,
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
async def test_a_resized_worker_is_undefined_before_it_is_redefined(monitor: Providers) -> None:
    request = await registration(monitor, build().domain)

    # `vcpu` and `memory` replace the domain rather than update it, and the
    # domain's name is stated rather than generated — so a replacement that
    # created first would collide with the domain it is replacing.
    assert request.deleteBeforeReplace is True
    assert request.deleteBeforeReplaceDefined is True


@pytest.mark.asyncio
async def test_the_only_domain_on_this_host_the_program_declares_is_the_worker(monitor: Providers) -> None:
    """The home-automation domain beside it is nobody's declaration here.

    It predates this program and outlives it, its full definition is the
    host's own configuration management's, and every way of declaring it is
    blind or destructive somewhere that matters (rfc-002 §13). A second domain
    appearing under this component is that decision being undone, so the count
    is asserted rather than left to a reader of the constructor.
    """
    async with declaring():
        _ = build()

    assert sorted(monitor.names('libvirt:index/domain:Domain')) == [DOMAIN]


@pytest.mark.asyncio
async def test_a_second_worker_is_a_design_change_not_a_default() -> None:
    with pytest.raises(ValueError, match='exactly one worker VM'):
        build(cluster=build_cluster(worker_nodes=(WORKER, 'worker2')))


# -- the session the provider dials through ---------------------------------
#
# The parameter spellings asserted below are the bridged Terraform provider's
# own (`libvirt/uri/ssh.go`), not libvirt's remote driver's: the provider parses
# the URI itself and opens the connection with Go's SSH client. Getting one of
# them wrong is silent — an unread parameter leaves the run on the machine's
# ambient defaults — so each is pinned by a test rather than by memory.

HOST = '10.144.200.1'
IDENTITY = '-----BEGIN OPENSSH PRIVATE KEY-----\nexample\n-----END OPENSSH PRIVATE KEY-----\n'


def _dial(tmp_path: Any, host: str = HOST) -> tuple[Any, dict[str, list[str]]]:
    """The parsed URI and its parameters, for a session in a checkout at `tmp_path`."""
    from kluster.components.homelab import connection_uri

    uri = connection_uri(host=host, private_key=IDENTITY, root=tmp_path)
    parts = urlsplit(uri)
    return parts, parse_qs(parts.query)


def _opened(tmp_path: Path, value: str) -> Path:
    """The file a parameter names, as the provider resolves it.

    The values in the URI are relative, and what they are relative to is the
    plugin process's working directory -- the checkout root. Resolving them the
    same way here is what makes the assertions about file contents assertions
    about the file the provider would actually open.
    """
    resolved = tmp_path / value
    assert not Path(value).is_absolute(), f'{value!r} is absolute, and would differ on every machine'
    return resolved


def test_the_endpoint_names_the_service_user_and_the_privileged_daemon(tmp_path: Path) -> None:
    parts, _ = _dial(tmp_path)

    # `/system` is the daemon that owns the storage pool and the domains on
    # this host; a session instance would see neither.
    assert (parts.scheme, parts.netloc, parts.path) == ('qemu+ssh', f'virt@{HOST}', '/system')


def test_the_identity_is_materialised_where_only_this_machine_can_read_it(tmp_path: Path) -> None:
    """The credential is configuration; the file it becomes is not.

    A path is a property of the machine running the program, and this program
    runs on a workstation and on a continuous-integration runner alike. So the
    file is written on every run, from the configured key, into the checkout's
    own directory — and the URI is what points the provider at it.
    """
    _, query = _dial(tmp_path)

    keyfile = _opened(tmp_path, query['keyfile'][0])
    assert keyfile.read_text() == IDENTITY
    # The mode matters as much as the content: a private key readable by
    # anything else on the machine is a leaked private key.
    assert keyfile.stat().st_mode & 0o777 == 0o600
    assert keyfile.parent.stat().st_mode & 0o777 == 0o700


def test_the_pin_is_written_against_the_address_the_session_dials(tmp_path: Path) -> None:
    """A `known_hosts` entry is keyed by host, so the key it is keyed by matters.

    A device file stores the pin as a bare `ssh-ed25519 <blob>` with no host name
    in front of it, which is what lets one pinned key match a device at either
    of its addresses. A `known_hosts` file has no such form: the address the
    URI dials is written in front of the blob here, at the moment the endpoint
    that decides it is derived.
    """
    from kluster.conventions import HOMELAB_HOST_KEY

    _, query = _dial(tmp_path)

    assert _opened(tmp_path, query['knownhosts'][0]).read_text() == f'{HOST} {HOMELAB_HOST_KEY}\n'
    assert HOMELAB_HOST_KEY.startswith('ssh-ed25519 ')


def test_host_key_verification_is_not_switched_off(tmp_path: Path) -> None:
    """Two ways to lose it, and the test exists for the second one.

    `no_verify` disables verification by its mere presence — even
    `no_verify=0` — so it is never emitted. And the parameter that names the
    file is `knownhosts`, one word: spelled `known_hosts` it is simply not
    read, the run falls back on `$HOME/.ssh/known_hosts`, and the session
    trusts whatever that machine happens to have collected.
    """
    parts, query = _dial(tmp_path)

    assert 'no_verify' not in parts.query
    assert 'known_hosts' not in query
    assert query['knownhosts']


def test_the_session_offers_the_identity_it_was_given_and_no_other(tmp_path: Path) -> None:
    """The provider's default is `agent,privkey`, and an agent is ambient.

    A forwarded agent would offer its keys before this one, so what a
    continuous-integration runner does and what a workstation does would differ
    by whatever the operator happened to have loaded.
    """
    _, query = _dial(tmp_path)

    assert query['sshauth'] == ['privkey']


def test_a_session_with_no_identity_is_refused(tmp_path: Path) -> None:
    from kluster.components.homelab import connection_uri

    with pytest.raises(ValueError, match='identity is empty'):
        _ = connection_uri(host=HOST, private_key='   \n', root=tmp_path)


def test_a_checkout_the_provider_would_rewrite_the_path_of_is_refused(tmp_path: Path) -> None:
    """`$` in a path is expanded by the provider before the file is opened.

    The failure that would otherwise follow arrives inside an SSH handshake,
    as an authentication that got no key, which names nothing about why.
    """
    from kluster.components.homelab import connection_uri

    with pytest.raises(ValueError, match=r'expands'):
        _ = connection_uri(host=HOST, private_key=IDENTITY, root=tmp_path / 'build$one')


def test_the_slot_is_the_checkouts_own_local_directory() -> None:
    """Not an absolute path on one machine: a directory inside the checkout.

    It is the same directory every other local half of a credential lives in
    (`credentials.md` §1 rule 6), which is what makes "what on this machine is
    secret" have one answer.
    """
    from kluster.components.homelab import SLOT, slot
    from kluster.lib import workstation

    assert slot() == workstation.directory() / SLOT
    assert slot().is_relative_to(workstation.repo_root())


# -- the disk tuning the provider cannot express ----------------------------


def test_the_data_disk_returns_freed_space_to_the_host() -> None:
    from kluster.components.homelab import DISK_FORMAT, disk_tuning_xslt

    disk = _transformed_disk("disk[@device='disk']")

    # `discard=unmap` is what makes in-guest TRIM punch holes back out of the
    # sparse image; without it the file only ever grows, and the migration
    # interleaves reclamation with growth on a disk that has no slack.
    assert disk.attrib['discard'] == 'unmap'
    # `cache=none` keeps guest I/O out of the host page cache, which would
    # otherwise cache it a second time.
    assert disk.attrib['cache'] == 'none'
    assert disk.attrib['type'] == 'raw'
    # The stylesheet is read verbatim, so the format it writes is a literal in
    # the file rather than something the volume hands it. The two must agree:
    # a driver naming a format the volume was not created with is a domain that
    # will not start.
    assert f'type="{DISK_FORMAT}"' in disk_tuning_xslt()


def test_the_seed_is_left_exactly_as_the_provider_wrote_it() -> None:
    # The stylesheet is a scalpel: the cdrom wants neither setting, and a
    # transform that reached it would be changing a disk it does not
    # understand.
    cdrom = _transformed_disk("disk[@device='cdrom']")

    assert 'discard' not in cdrom.attrib
    assert 'cache' not in cdrom.attrib


async def registration(monitor: Providers, resource: pulumi.Resource) -> Any:
    """The options a resource was registered with, once it has been.

    Registration is a background task, so a component's constructor returning
    is not the moment its resources reached the monitor; resolving the URN is.
    """
    return monitor.options_of(str(await resource.urn.future()).rsplit('::', 1)[-1])


def _transformed_disk(selector: str) -> Any:
    """One disk's driver element, after the domain XML has been through XSLT."""
    from kluster.components.homelab import disk_tuning_xslt

    etree = pytest.importorskip('lxml.etree', reason='lxml is present as a dependency of pykeepass')
    transform = etree.XSLT(etree.XML(disk_tuning_xslt().encode()))
    result = transform(etree.XML(PROVIDER_DOMAIN_XML.encode()))
    driver = result.getroot().find(f'devices/{selector}/driver')
    assert driver is not None
    return driver


def test_the_paths_in_the_uri_are_relative_to_the_checkout(tmp_path: Path) -> None:
    """An absolute path here is a diff that can never be resolved (rfc-002 §8.4).

    The URI is a provider input and therefore lives in state, so an absolute
    path would record the path one machine happened to have and every other
    machine would then propose changing it. The provider opens both values
    without anchoring them, and the anchor it falls back on is the plugin
    process\'s working directory -- the project root, since this project
    declares no `main`.
    """
    from kluster.components.homelab import KEYFILE, KNOWN_HOSTS, SLOT
    from kluster.lib import workstation

    _, query = _dial(tmp_path)

    assert query['keyfile'] == [f'{workstation.DIRECTORY}/{SLOT}/{KEYFILE}']
    assert query['knownhosts'] == [f'{workstation.DIRECTORY}/{SLOT}/{KNOWN_HOSTS}']


@pytest.mark.asyncio
async def test_the_session_credential_is_read_where_the_provider_is_built(tmp_path: Path) -> None:
    """The key configures this provider and reaches nothing else.

    A credential that exists only to open a connection is read at the line that
    opens it (rfc-002 §8.1), so it is not a parameter of the component. Where
    the connection goes is: `host` is an ordinary input, and the stack program
    reads it off the roster.
    """
    from kluster.components import homelab

    host = build()
    uri = cast('str | None', await host.provider.uri.future())
    assert uri is not None
    address = conventions.overlay.member(conventions.overlay.MEMBER_HOMELAB).address
    assert urlsplit(uri).netloc == f'{homelab.LIBVIRT_USER}@{address}'
    keyfile: str = parse_qs(urlsplit(uri).query)['keyfile'][0]
    assert _opened(tmp_path, keyfile).read_text() == IDENTITY

    parameters = inspect.signature(homelab.HomelabHost.__init__).parameters
    assert 'private_key' not in parameters
    assert 'uri' not in parameters


@pytest.mark.asyncio
async def test_every_domain_and_volume_is_signed_by_the_hosts_own_provider(monitor: Providers) -> None:
    """Inherited from the component, not re-plumbed onto each child.

    The pool, the disk, the seed and the worker domain are all children of the
    component that built the provider, so each takes it from its parent\'s
    provider map.
    """
    async with declaring():
        _ = build()

    libvirt = [d for d in monitor.declared if d.typ.startswith('libvirt:')]
    assert libvirt, 'the build declared no libvirt resources at all'
    for declaration in libvirt:
        assert f'{CLUSTER}-libvirt' in declaration.provider, f'{declaration.name} is not signed by the host provider'
