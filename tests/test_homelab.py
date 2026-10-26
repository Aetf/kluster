"""The worker VM under libvirt, and the session that declares it.

The worker is ordinary infrastructure — it can be rebuilt — but the disk
under it is not. What keeps the program from resizing or rewriting that disk
out from under a running node is resource options, and each carries its reason
at its declaration (`components/homelab`) rather than a case here restating it.
The cases hold what the declarations produce: the disk and the seed the
worker boots, the session the provider dials, and what the stylesheets make of
the definitions the provider generates. What the engine does with the seed's
options is `tests/test_homelab_engine.py`'s.

The home-automation domain on the same host is declared nowhere, so nothing
here asserts about it (rfc-002 §13).
"""

from __future__ import annotations

import copy
import importlib.metadata
import inspect
import io
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pinned_tools
import process_sessions
import pulumi
import pycdlib
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with
from pycdlib.dr import DirectoryRecord

from kluster import conventions
from kluster.components import homelab

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

#: The domain the pinned provider generates for this declaration, before its
#: stylesheet, as its debug output records it. The stylesheet cases run over it,
#: so what is asserted is the XML libvirt would receive.
TRANSCRIPT = Path(__file__).parent / 'data' / 'homelab-domain.xml'

#: A volume as the pinned provider generates one before its stylesheet, as its
#: debug output records it (`Generated XML for libvirt volume`): the disk's,
#: and every volume's has this shape, with the provider's own `644`. Part of
#: the stylesheet layer with the domain's transcript, and recorded again with
#: it when the pinned provider moves.
PROVIDER_VOLUME_XML = """<volume>
    <name>kluster-worker.raw</name>
    <capacity unit="B">4194304</capacity>
    <target>
        <format type="raw"></format>
        <permissions>
            <mode>644</mode>
        </permissions>
    </target>
</volume>
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
    # `.credentials/` and names both files relative to the checkout in the
    # connection URI, so every case here runs in a checkout of its own rather
    # than in a layer alone: the running checkout's layer is closed, and a
    # write into it is refused (framework/testing.md §1.1).
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
        bgp_peers={},
        # Any image reference serves: no case here reads the installer, and
        # every node has to name one.
        installers={node: f'factory.talos.dev/installer/{node}:v1.11.0' for node in ('cp1', *worker_nodes)},
        volumes={},
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

    assert await host.volume.format.future() == homelab.DISK_FORMAT
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
    # volume's capacity from its source file — so stating the worker's
    # intended disk size here would not shrink-wrap anything, it would fail
    # the apply.
    assert 'size' not in dict(request.object)
    assert await host.volume.size.future() is None


@pytest.mark.asyncio
async def test_the_domain_boots_the_seed_as_its_second_disk_and_carries_no_cloud_init_cd() -> None:
    """The q35 machine has no IDE bus, and the provider's `cloudinit` attaches the seed on one."""
    host = build()

    assert await host.domain.cloudinit.future() is None
    disks = await host.domain.disks.future()
    assert disks is not None
    assert [disk.volume_id for disk in disks] == [await host.volume.id.future(), await host.seed.id.future()]


@pytest.mark.asyncio
async def test_the_seed_is_a_cidata_image_carrying_the_rendering_and_the_derived_metadata(tmp_path: Path) -> None:
    """What Talos' `nocloud` platform finds: the label, the two files under their names, and nothing else.

    Read back through both name extensions, since a reader may take either.
    The metadata is derived from the node and static, because Talos reads it
    at every boot. There is no `network-config`: an empty one fails to decode,
    and the node's addressing is in its machine configuration.
    """
    host = build()

    iso = _opened_seed(tmp_path / str(await host.seed.source.future()))
    try:
        assert iso.pvd.volume_identifier.decode().rstrip() == 'cidata'
        assert _names(iso, rr_path='/') == ['meta-data', 'user-data']
        assert _names(iso, joliet_path='/') == ['meta-data', 'user-data']
        assert _read(iso, '/user-data') == MACHINE_CONFIG
        assert json.loads(_read(iso, '/meta-data')) == {'instance-id': DOMAIN, 'local-hostname': DOMAIN}
    finally:
        iso.close()


@pytest.mark.asyncio
async def test_the_seed_is_written_into_the_slot_where_only_this_machine_can_read_it(tmp_path: Path) -> None:
    """It carries the cluster's secrets, as the session's identity does, and sits beside it.

    The volume names it relative to the checkout, because the name is a
    provider input in state and an absolute path differs on every machine.
    """
    host = build()

    source = str(await host.seed.source.future())
    seed = _opened(tmp_path, source)
    assert seed.parent == homelab.slot(tmp_path)
    assert seed.stat().st_mode & 0o777 == 0o600
    assert seed.parent.stat().st_mode & 0o777 == 0o700


def test_one_rendering_names_one_seed_and_a_new_one_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name follows what the seed carries, not the image, whose bytes carry the time it was built.

    A name that moved on every run would be a new `source` on every run; one
    that stayed put across renderings would leave a stale seed under the
    current name. Each build here runs a minute after the one before it.
    """
    from kluster.components.homelab import seed

    clock = iter(range(1_800_000_000, 1_800_100_000, 60))
    monkeypatch.setattr(time, 'time', lambda: float(next(clock)))
    first = seed.write(tmp_path, user_data='one', meta_data='{}')
    again = seed.write(tmp_path, user_data='one', meta_data='{}')
    assert again == first

    second = seed.write(tmp_path, user_data='two', meta_data='{}')

    assert second != first
    assert sorted(tmp_path.iterdir()) == [second]


def _opened_seed(path: Path) -> pycdlib.PyCdlib:
    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(path.read_bytes()))
    return iso


def _names(iso: pycdlib.PyCdlib, **path: str) -> list[str]:
    """The names of the files at a directory, in the tree the keyword names: `rr_path` or `joliet_path`."""
    names: list[str] = []
    # pycdlib's own annotation leaves the records' type out of the generator.
    children = cast('Iterator[DirectoryRecord]', iso.list_children(**path))  # pyright: ignore[reportUnknownMemberType]
    for record in children:
        if record.is_dot() or record.is_dotdot():
            continue
        if 'rr_path' in path:
            assert record.rock_ridge is not None
            names.append(record.rock_ridge.name().decode())
        else:
            names.append(record.file_identifier().decode('utf-16-be'))
    return sorted(names)


def _read(iso: pycdlib.PyCdlib, path: str) -> str:
    sink = io.BytesIO()
    iso.get_file_from_iso_fp(sink, rr_path=path)
    return sink.getvalue().decode()


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
    # One NIC, and only one: the worker's machine configuration names its one
    # physical link by an alias that a second link would leave unassigned,
    # and the node would boot with no address (`talos.uplink_alias_document`).
    assert len(interfaces) == 1

    interface = interfaces[0]
    assert interface.bridge == BRIDGE
    # A real bridge, not macvtap: host-to-guest traffic is load-bearing here
    # (the NAS role serves NFS into the cluster), and macvtap is exactly the
    # path that cannot hairpin back to the host.
    assert interface.macvtap is None
    assert interface.network_name is None


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
    assert (parts.scheme, parts.netloc, parts.path) == ('qemu+ssh', f'{homelab.LIBVIRT_USER}@{HOST}', '/system')


def test_the_identity_is_materialized_where_only_this_machine_can_read_it(tmp_path: Path) -> None:
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


# -- what the stylesheets make of the provider's definitions ----------------
#
# Each stylesheet is applied by the pinned `xsltproc`, with the flags the
# provider runs it with (`libvirt/utils_xslt.go`), to what the provider
# generates, so the result is what libvirt receives.


def test_the_transcript_is_the_installed_providers() -> None:
    """The transcript stands for the pinned provider, and a new provider generates its own.

    A bump of `pulumi-libvirt` is finished when the transcript is recorded
    again from it (its own header says how) and the cases below pass over the
    new one, and the stylesheet layer is gone when the provider declares what
    it works around (`components.homelab`).
    """
    recorded = re.search(r'pulumi-libvirt (\S+) generates', TRANSCRIPT.read_text())
    assert recorded is not None
    assert importlib.metadata.version('pulumi-libvirt') == recorded.group(1), (
        'pulumi-libvirt moved: record tests/data/homelab-domain.xml and PROVIDER_VOLUME_XML again from it, '
        'and check templates/domain.xslt.j2 and templates/volume.xslt against them'
    )


@pytest.mark.asyncio
async def test_the_domain_stylesheet_makes_exactly_its_three_changes(tmp_path: Path) -> None:
    """The data disk gets its driver, the seed goes read-only, the display goes, and nothing else moves.

    The data disk is driven in the format its volume is created in, with
    `cache=none` and `discard=unmap` (homelab-host.md §1). The seed is
    read-only, so Talos leaves it out of provisioning. The provider's SPICE
    display is one the host's QEMU cannot offer.
    """
    host = build()
    xml = await host.domain.xml.future()
    assert xml is not None and xml.xslt is not None
    etree = _etree()
    provided = etree.XML(_without_comments(TRANSCRIPT.read_text()).encode())
    # A third disk, as the data disk under another name and in another
    # format: each template matches its own volume and nothing wider, so this
    # one comes through as the provider wrote it.
    other = copy.deepcopy(provided.find('devices/disk'))
    other.find('driver').set('type', 'qcow2')
    other.find('source').set('volume', 'another.qcow2')
    other.find('target').set('dev', 'vdc')
    provided.find('devices').insert(2, other)

    transformed = etree.XML(_xsltproc(tmp_path, xml.xslt, etree.tostring(provided)))

    expected = copy.deepcopy(provided)
    data_disk, seed_disk, _ = expected.findall('devices/disk')
    assert data_disk.find('source').get('volume') == await host.volume.name.future()
    assert seed_disk.find('source').get('volume') == await host.seed.name.future()
    driver = data_disk.find('driver')
    driver.attrib.clear()
    driver.attrib.update(
        {'name': 'qemu', 'type': str(await host.volume.format.future()), 'cache': 'none', 'discard': 'unmap'}
    )
    seed_disk.append(etree.Element('readonly'))
    devices = expected.find('devices')
    devices.remove(devices.find('graphics'))
    assert _canonical(transformed) == _canonical(expected)


@pytest.mark.asyncio
async def test_each_volume_is_created_readable_by_its_owner_alone(tmp_path: Path) -> None:
    """The disk and the seed carry the machine secrets, and the provider creates every volume `0644`.

    The mode is all the stylesheet changes; the directory above the files is
    the host's, and QEMU needs its search bit.
    """
    host = build()
    etree = _etree()
    expected = etree.XML(PROVIDER_VOLUME_XML.encode())
    expected.find('target/permissions/mode').text = '0600'

    for volume in (host.volume, host.seed):
        xml = await volume.xml.future()
        assert xml is not None and xml.xslt is not None
        transformed = etree.XML(_xsltproc(tmp_path, xml.xslt, PROVIDER_VOLUME_XML.encode()))
        assert _canonical(transformed) == _canonical(expected)


def _xsltproc(tmp_path: Path, stylesheet: str, document: bytes) -> bytes:
    """`document` through `stylesheet`, as the provider runs the pinned `xsltproc` over a definition."""
    style = tmp_path / 'style.xslt'
    _ = style.write_text(stylesheet)
    source = tmp_path / 'definition.xml'
    _ = source.write_bytes(document)
    ran = process_sessions.run(
        [pinned_tools.located('xsltproc'), '--nomkdir', '--nonet', '--nowrite', str(style), str(source)],
        timeout=30,
    )
    assert ran.returncode == 0, ran.stderr
    return ran.stdout


def _without_comments(text: str) -> str:
    return re.sub(r'<!--.*?-->\s*', '', text, flags=re.S)


def _canonical(element: Any) -> tuple[Any, ...]:
    """An element as its tag, attributes, text and children, with the indentation between elements dropped."""
    return (
        element.tag,
        sorted(element.attrib.items()),
        (element.text or '').strip(),
        [_canonical(child) for child in element],
    )


async def registration(monitor: Providers, resource: pulumi.Resource) -> Any:
    """The options a resource was registered with, once it has been.

    Registration is a background task, so a component's constructor returning
    is not the moment its resources reached the monitor; resolving the URN is.
    """
    return monitor.options_of(str(await resource.urn.future()).rsplit('::', 1)[-1])


def _etree() -> Any:
    return pytest.importorskip('lxml.etree', reason='lxml is present as a dependency of pykeepass')


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

    (provider,) = [d.name for d in monitor.declared if d.typ == 'pulumi:providers:libvirt']
    libvirt = [d for d in monitor.declared if d.typ.startswith('libvirt:')]
    assert libvirt, 'the build declared no libvirt resources at all'
    for declaration in libvirt:
        assert f'::{provider}::' in declaration.provider, f'{declaration.name} is not signed by the host provider'
