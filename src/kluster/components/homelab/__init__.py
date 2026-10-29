"""The homelab side of the physical layer, under libvirt (physical.md §3).

One domain on this host is this program's: the Talos worker VM, with the
storage pool, disk and seed volume it boots from. The home-automation
domain that shares the host is **not declared here** — it predates this
program, outlives it, and belongs to the host's own configuration management
(rfc-002 §13).

The worker's machine configuration reaches it on a seed volume rather than
through a metadata service: there is no cloud platform here to serve one. The
seed is an ISO 9660 image labelled `cidata` that the program builds (`seed`),
uploaded into the pool as an ordinary volume and attached as the domain's
second virtio disk, read-only. It carries cluster secrets, as the disk does,
and both volumes are declared `0600` (`volume_xslt`). Neither the provider's
`CloudInitDisk` nor the domain's `cloudinit` attribute is used: the attribute
writes the seed as an IDE cdrom, which the q35 machine has no bus for.

**The seed is first-boot input.** Talos reads its `user-data` only while the
disk's STATE partition holds no complete configuration, so the seed ignores
later renderings, and the day-1 chain's `ConfigurationApply` keeps the running
worker current as it does the control planes. It is replaced with the disk,
so a new disk boots the rendering current at the run that created it.

**The connection is derived, and its credential is materialized** rather than
recorded: an SSH transport is authenticated by files on the machine running
the program, and their paths are a property of that machine rather than of the
site. `connection_uri` writes both into the checkout and returns the URI that
names them, so a workstation and a continuous-integration runner reach the
host the same way from the same configuration.

**The disk is created from the Talos image, not created empty.** The volume's
`source` is the decompressed `nocloud` artifact on the machine running the
program (`providers/talos_factory/`), and the provider uploads it into the
pool over the same connection it defines the domain through. So the first
boot is a consequence of an apply rather than an operator writing an image
by hand.

The system this assumes of the host — the disk shape, the second bridge, the
two-phase GPU passthrough, and the host preparation that must happen before
any of it — is docs/physical/homelab-host.md. This module owns only the
declaration.

**What the provider makes irreversible.** Every field of a libvirt volume
forces a new volume, `size` included, and the volume resource has no update
path at all; the domain's `vcpu` and `memory` force a new domain. So the two
growth steps the migration plans for (migration.md §0.4) are not the same
operation:

-   *RAM* grows by editing the declared size and applying, which stops the
    domain, redefines it and starts it again. The disk is a separate resource
    and survives; the workloads on it do not, so the growth belongs in a
    drained window.
-   *Disk* grows on the host — `truncate` plus `virsh blockresize`, and Talos
    extends its EPHEMERAL partition into the new space (homelab-host.md §1).
    The declaration cannot state a size at all: the provider refuses `size`
    beside `source` and sets the volume's capacity from the source file's
    length. That file is the Talos image followed by the room Talos needs to
    create its data partitions at first boot
    (`components/talos/image.py`, `HOMELAB_DISK_ROOM`), so the disk is
    created bootable and its working size is the host-side step's to reach.
    The file and the declaration part company from the moment that step runs —
    which is why `size` is ignored here as well. Every field of a libvirt
    volume replaces the volume, so a program insisting on a size would propose
    destroying the worker's disk the first time a refresh read the grown file
    back.

The image is a creation-time fact for the same reason. Talos upgrades itself
in place over its machine API — the declared artifact is what the disk was
*written* with, not what is on it now — so the declaration stops describing the
volume the first time the node is upgraded, and a later `versions:talos` bump
must not propose rewriting a running node's disk. Rebuilding the worker from a
newer image is therefore a deliberate act rather than a diff, and two
commands, with `physical` the function of declarative/physical.md §6, which
runs `pulumi` under the stack's own passphrase: `physical state unprotect
'<disk URN>'`, then `physical up --replace '<image URN>' --replace
'<disk URN>'`, the image being the worker's `FactoryImage`. The provider opens
the disk's `source` only at the create, after the old disk is deleted, and the
image's replacement, created first, puts the file there when the machine
running the apply has lost it (operations.md §3.2). The run
deletes the disk, the seed and the domain, the disk last, before it creates
any of them again, the seed from the current rendering; the program's
`protect` protects the new disk in the same run. It
destroys everything the node held, which is why nothing does it by accident.
A run that replaces the disk while no file is at its `source` is refused at
the disk's delete, once the seed and the domain are gone, and keeps the disk
(`rebuild`); a plain `up` then creates those two again on it.

**What the provider cannot declare is one stylesheet layer**, applied when the
domain and the volumes are created: the data disk's cache and discard, the
seed's read-only flag, a domain with no display, and a volume's mode
(`domain_xslt`, `volume_xslt`). The provider applies an `xml.xslt` by running
`xsltproc` on the machine running the program, pinned in `mise.toml`. The
layer goes when the pinned provider is a release whose schema declares those
four and whose SSH transport offers only the configured identity
(homelab-host.md §6): the templates, the pin, the provider's recorded
definitions — the domain's transcript `tests/data/homelab-domain.xml` and the
volume's `PROVIDER_VOLUME_XML` in `tests/test_homelab.py` — and the cases over
them go with it, and the declarations take the provider's own attributes in
their place. A case fails when the installed provider is not the release the
transcript was recorded from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pulumi
import pulumi_libvirt as libvirt

from kluster import conventions
from kluster.components.talos import TalosCluster
from kluster.lib import templates, workstation
from putils import Component, async_output, own_provider_opts, resolve, with_provider

from . import seed
from .rebuild import SourceGuard

__all__ = ('LIBVIRT_USER', 'PRIVATE_KEY', 'HomelabHost', 'connection_uri', 'slot')

#: The package `importlib.resources` resolves this module's `templates/`
#: directory against, so the stylesheets travel with the code that reads them
#: (rfc-002 §9.1).
_TEMPLATE_PACKAGE = 'kluster.components.homelab'

#: Memory is quoted in GiB (nodes.md §4.2) and libvirt domains are sized in
#: MiB. Disk sizes have no counterpart here: a volume created from a source
#: file takes that file's length, and the rest is host-side growth.
MIB_PER_GIB = 1024

#: Raw, not qcow2: the image lives on a nodatacow subvolume, where qcow2's
#: allocation layer buys indirection and nothing else (homelab-host.md §1).
#: The volume is created sparse — the provider declares a capacity and no
#: allocation — so the file occupies what the guest has written.
DISK_FORMAT = 'raw'

#: The seed's format as libvirt records it. libvirt probes an uploaded ISO
#: image as `iso` and the volume's read returns that, so a seed declared `raw`
#: would differ from its own read on every refresh, and every field of a
#: libvirt volume replaces the volume. The disk that attaches it is still
#: driven `raw`, the provider's driver for every format but qcow2.
SEED_FORMAT = 'iso'

#: Q35 rather than the provider's default i440fx. The machine type is fixed
#: at definition time, and the Wave C cutover hands this domain a PCI hostdev
#: (homelab-host.md §3) — PCIe, which i440fx does not have.
MACHINE_TYPE = 'q35'

#: The host CPU as it is. The domain has no migration target — there is one
#: host — so hiding the CPU's features behind a portable model would cost the
#: guest instruction sets and buy nothing.
CPU_MODE = 'host-passthrough'

#: The account the session authenticates as: a dedicated service user on the
#: host, in the `libvirt` group and no other, provisioned together with its key
#: by the host's own configuration management (homelab-host.md §4).
LIBVIRT_USER = 'virt'

#: The workstation slot the run materializes its transport and the worker's
#: seed into (credentials.md §1 rule 6): the checkout's git-ignored
#: `.credentials/`, one directory deeper so these files sit beside another
#: consumer's rather than in the root of it.
SLOT = 'libvirt'
KEYFILE = 'identity'
KNOWN_HOSTS = 'known_hosts'

#: The libvirt driver and the object the URI names on the far side. `/system`
#: is the privileged daemon — the one that owns the storage pool and the
#: domains on this host — as opposed to a per-user session instance.
LIBVIRT_ENDPOINT = 'qemu+ssh'
LIBVIRT_OBJECT = '/system'

#: The client half of the libvirt session, and the only part of that session
#: stack configuration carries. It is read where the provider is built and
#: nowhere else (rfc-002 §8.1): where it lands on the machine running the
#: program, and the URI that names it there, are derived.
PRIVATE_KEY = 'libvirtPrivateKey'


def slot(root: Path | None = None) -> Path:
    """`.credentials/libvirt/` in this checkout.

    Every file in it is a **working file, not a slot**: a slot is durable
    and written by a `credentials` command — a kit, a passphrase, a client
    bundle — while these — the session's identity and `known_hosts`, and
    the worker's seed image — are derived from stack configuration by this
    component on every run, owned by it and disposable. They live under
    `.credentials/` anyway because that directory is the `0700` boundary, the
    one answer to "what on this machine is secret", and a second git-ignored
    directory would be a second boundary to get right. Nothing here is
    produced by a command, so nothing should go looking for the command that
    produced it.
    """
    return (workstation.directory() if root is None else workstation.slots(root)) / SLOT


def connection_uri(*, host: str, private_key: str, root: Path | None = None) -> str:
    """Materialize the SSH transport and return the URI that names it.

    The endpoint is **derived, never recorded**. A URI in stack configuration
    would carry file paths that only exist on the machine that wrote it, and
    this program runs on a workstation and on a continuous-integration runner
    alike; what is configuration is the credential (`libvirtPrivateKey`), and
    where it lands is decided here, relative to the checkout.

    Two files are written before the provider is constructed, because the
    provider dials as soon as the engine configures it and reads both by path:

    -   the client identity, `0600` in a `0700` directory (`workstation`);
    -   a `known_hosts` file holding one line — `host` followed by the pinned
        `conventions.HOMELAB_HOST_KEY`. The pin is written against the address
        the URI dials, which is what the verifier matches on.

    **Both paths go into the URI relative to the checkout root** (rfc-002
    §8.4). The URI is a provider input and therefore lives in state, so an
    absolute path would put the path one machine happened to have into a value
    every other machine then diffs against — a diff that can never be resolved
    on a stack whose merge gate is a clean preview. The provider opens both
    values without anchoring them, so a relative one resolves against the
    plugin process's working directory, which Pulumi sets to the project's
    `main` directory where the project declares one and otherwise to the
    directory holding `Pulumi.yaml`. This project declares no `main`, so the
    two coincide and both are the checkout root — **adding a `main` later would
    move the anchor silently**, which is why the caveat is written here rather
    than assumed.

    What the provider honors here is not libvirt's own remote driver: the
    bridged Terraform provider parses the URI itself and dials over Go's SSH
    client (`libvirt/uri/ssh.go`). Three query parameters follow from that, and
    the spellings are the provider's rather than OpenSSH's:

    -   `keyfile` — the identity, read first and before anything an
        `~/.ssh/config` names.
    -   `knownhosts` — *one word*. `known_hosts` is not a parameter the
        provider reads: it would be ignored, the run would fall back on
        `$HOME/.ssh/known_hosts`, and the session would verify against
        whatever the machine happened to trust.
    -   `sshauth=privkey` — the default is `agent,privkey`, which would offer
        every key in a forwarded agent before this one. Naming the method is
        the same rule as the gateway's session: what a runner does and what a
        workstation does may not differ.

    `no_verify` is *never* emitted. The provider treats it as set by its mere
    presence — even `no_verify=0` disables host-key verification — and an
    unverified first contact over the overlay would hand an interposer a
    root-equivalent libvirt connection.

    `root` overrides the checkout, for tests. It may not contain `$`: the
    provider expands environment variables in both paths before opening them,
    so a `$` anywhere in the path the emitted values are derived from is
    refused here rather than corrupted deep inside an SSH handshake.
    """
    if not private_key.strip():
        raise ValueError('the libvirt SSH identity is empty, and an unauthenticated session reaches nothing')
    checkout = workstation.repo_root() if root is None else root
    target = slot(root)
    if '$' in str(target):
        raise ValueError(f'{target} contains a "$", which the libvirt provider expands before opening the file')

    keyfile = workstation.write(target / KEYFILE, private_key)
    known_hosts = workstation.write(target / KNOWN_HOSTS, f'{host} {conventions.HOMELAB_HOST_KEY}')
    query = urlencode(
        {
            'keyfile': str(keyfile.relative_to(checkout)),
            'knownhosts': str(known_hosts.relative_to(checkout)),
            'sshauth': 'privkey',
        }
    )
    return f'{LIBVIRT_ENDPOINT}://{LIBVIRT_USER}@{host}{LIBVIRT_OBJECT}?{query}'


@dataclass(frozen=True)
class _DomainStylesheet:
    """What the domain's stylesheet is rendered with: the volumes it tells apart."""

    disk_volume: str
    disk_format: str
    seed_volume: str


def domain_xslt(*, disk_volume: str, seed_volume: str) -> str:
    """The stylesheet that turns the provider's domain into the one the worker runs.

    Three changes, each matched by what it changes and nothing wider:

    -   The data disk, matched by its volume's name, is driven `cache=none`
        and `discard=unmap`. Both are load-bearing (homelab-host.md §1):
        `discard=unmap` is what lets in-guest TRIM punch holes back out of the
        sparse image, so deleting data in the cluster returns NVMe to the host
        during the interleaved migration, and `cache=none` keeps guest I/O
        from being cached a second time in the host's page cache. The driver
        element is written rather than patched, so the result does not depend
        on which attributes the provider happened to emit, and it names the
        format the volume is created in.
    -   The seed, matched by its volume's name, is read-only to the guest.
        Talos leaves a read-only disk out of provisioning, and a writable seed
        would be a candidate for a node volume whose selector finds its disk
        by exclusion (`!system_disk`).
    -   The display is removed. The provider adds a SPICE display to a domain
        that declares none, and the host's QEMU has no SPICE: libvirt refuses
        the definition. The host is headless, and the serial console is how
        the worker says why it failed.

    The reasons are stated here rather than in the template, whose rendering
    is the domain's `xml.xslt` input: an edit to it, a comment included, is a
    change to the domain.
    """
    return templates.render(
        _TEMPLATE_PACKAGE,
        'templates/domain.xslt.j2',
        _DomainStylesheet(disk_volume=disk_volume, disk_format=DISK_FORMAT, seed_volume=seed_volume),
    )


def volume_xslt() -> str:
    """The stylesheet that gives a volume's file mode `0600`.

    The disk and the seed both carry the worker's machine secrets, and the
    provider creates every volume `0644`, readable by every account on the
    host that can reach the pool's directory. The directory is the host
    preparation's, and keeps the search bit for others that QEMU, which the
    host's system instance runs as `libvirt-qemu` rather than root, needs to
    reach any image in it, the home-automation domain's included; so the
    files are what keeps the secrets to their owner: `0600`, and owned by
    root except while QEMU holds them.

    `0600` and owned by root does not lock QEMU out of the worker's images.
    When libvirt's QEMU driver starts a domain, it sets "the ownership of the
    file/device path to the correct user/group ID" of each of its disk images
    (drvqemu.html, "POSIX users/groups"; `dynamic_ownership` in `qemu.conf`,
    on by default), so the image's owner bits become QEMU's. The disk returns
    to root when the domain stops (`remember_owner`), and the read-only seed
    stays QEMU's from the worker's first start on: "The user/group of
    read-only images is not restored as with read-write images" (`qemu.conf`,
    beside `dynamic_ownership`). So each image is readable by root and by the
    account every domain's QEMU runs as, and by no other.

    Read verbatim, for the reason `domain_xslt` gives; an edit to it is a
    change to both volumes, and every field of a libvirt volume replaces it.
    """
    return templates.load(_TEMPLATE_PACKAGE, 'templates/volume.xslt')


def seed_metadata(hostname: str) -> str:
    """The `meta-data` half of the nocloud seed.

    Talos reads it at every boot, for the platform's metadata, while it reads
    `user-data` only at a boot with an empty STATE, so it is derived from the
    node and static rather than generated. Emitted as JSON because JSON is
    YAML, and the program then needs no serializer to state two facts.
    """
    return json.dumps({'instance-id': hostname, 'local-hostname': hostname})


def seed_source(*, user_data: pulumi.Input[str], meta_data: str, root: Path | None = None) -> pulumi.Output[str]:
    """Write the seed for `user_data` into the slot, and return its path relative to the checkout.

    The file is a working file of the slot's (`slot`), written on every run
    like the session's identity and for the same reason: a path is a property
    of the machine running the program. It is named relative to the checkout,
    as the identity is (`connection_uri`), because the volume's `source` is a
    provider input in state.
    """

    async def written() -> str:
        rendered = await resolve(user_data)
        checkout = workstation.repo_root() if root is None else root
        path = seed.write(slot(root), user_data=rendered, meta_data=meta_data)
        return str(path.relative_to(checkout))

    return async_output(written)


class HomelabHost(Component, pulumi_type='kluster:homelab:HomelabHost'):
    """The worker VM on the homelab host, and the storage it boots from.

    The home-automation domain beside it is **not declared** — this component
    reaches the host, and states nothing about a machine it did not build
    (rfc-002 §13).

    The libvirt session is this component's own, so the credential that opens
    it — `libvirtPrivateKey` — is read here, at the line that builds the
    provider (rfc-002 §8.1), and appears in no signature above or below. Only
    the credential: where the host answers is an ordinary input, and the rest
    of the URI is a property of the machine running the program
    (`connection_uri`).

    :param host: where the libvirt session dials the host — the address the
        overlay roster assigns it. A parameter like every other fact about the
        machine, because a component receives what it is declared against.
    :param cluster: the day-0 chain. The worker's configuration and the
        secrets the seed carries come out of the same place, so the component
        takes the cluster whole rather than a rendered string.
    :param storage_dir: the nodatacow subvolume that holds the disk image and
        the seed. The pool that points at it is declared here; the subvolume,
        its mode and its `chattr +C` are host preparation (homelab-host.md
        §4).
    :param image_path: the decompressed Talos `nocloud` image, on the machine
        running the program. The provider reads it there and uploads it into
        the pool; nothing about it is fetched by the host.
    """

    def __init__(
        self,
        name: str,
        *,
        cluster: TalosCluster,
        host: str,
        storage_dir: pulumi.Input[str],
        bridge: pulumi.Input[str],
        vcpus: int,
        memory_gib: int,
        image_path: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        # The session this component's resources are declared through. It is
        # built before the component registers, because a provider reaches a
        # subtree through the options the component is registered with.
        #
        # The identity is read in the clear rather than as a secret Output: it
        # is written to a file before any resource exists, so it reaches no
        # resource input and therefore no state.
        provider = libvirt.Provider(
            f'{name}-libvirt',
            uri=connection_uri(
                host=host,
                private_key=pulumi.Config().require(PRIVATE_KEY),
            ),
            opts=own_provider_opts(opts),
        )
        super().__init__(name, opts=with_provider(opts, provider))
        self.provider = provider
        node = _sole_worker(cluster)
        domain_name = f'{name}-{node}'

        # Names are stated rather than left to Pulumi's auto-naming, because
        # these are not opaque handles: they are what `virsh` lists and what
        # the files on the host are called.
        self.pool = libvirt.Pool(
            f'{name}-pool',
            name=name,
            type='dir',
            target=libvirt.PoolTargetArgs(path=storage_dir),
            # A dir pool's deletion is the directory's deletion, and this
            # directory is the worker's disk.
            opts=self.child_opts(protect=True),
        )

        disk_volume = f'{domain_name}.{DISK_FORMAT}'
        # What keeps a replacement from deleting the disk while no file is at
        # its `source` (`rebuild`).
        guard = SourceGuard()
        self.volume = libvirt.Volume(
            f'{domain_name}-disk',
            name=disk_volume,
            pool=self.pool.name,
            format=DISK_FORMAT,
            # The bytes the worker boots. No `size` beside it: the provider
            # refuses the pair and takes the volume's capacity from the file,
            # which is the image and the room Talos boots into after it.
            source=guard.source(image_path),
            xml=libvirt.VolumeXmlArgs(xslt=volume_xslt()),
            opts=self.child_opts(
                # The worker's disk is everything the node holds, and every
                # field of a libvirt volume replaces it, so neither a delete
                # nor a replacement happens by accident. Rebuilding the worker
                # from a newer image is the deliberate unprotect and replace
                # the module docstring describes.
                protect=True,
                # Both are facts about the volume's *creation*, and every field
                # of a libvirt volume replaces the volume, so neither may
                # become a diff afterwards:
                #
                # -   `size` is the source file's at creation and the host's
                #     from the first `truncate` onwards.
                # -   `source` describes the bytes the disk was written with,
                #     and stops describing what is on it the moment Talos
                #     upgrades itself over the machine API. Insisting on it
                #     would turn a routine `versions:talos` bump — which this
                #     stack makes for the machine configuration anyway — into a
                #     proposal to rewrite a running node's disk, which `protect`
                #     would then refuse for as long as the bump stood.
                ignore_changes=['size', 'source'],
                # The volume's name is stated, and a create before the delete
                # would ask the pool for a second volume under it: the
                # provider adopts the existing volume, uploads into it, and
                # then deletes it as the original, which leaves the pool with
                # no disk at all.
                delete_before_replace=True,
                # Deleted first and created from a file the provider opens
                # only at the create, the disk is not deleted while nothing is
                # there. The hook's name is recorded in state (`SourceGuard`).
                hooks=guard.hooks(f'{domain_name}-disk-source'),
            ),
        )

        # Talos' nocloud platform reads its machine configuration from the
        # seed's user-data, which is why the worker needs no metadata service.
        seed_volume = f'{domain_name}-seed.{SEED_FORMAT}'
        self.seed = libvirt.Volume(
            f'{domain_name}-seed',
            name=seed_volume,
            pool=self.pool.name,
            format=SEED_FORMAT,
            source=seed_source(user_data=cluster.machine_configs[node], meta_data=seed_metadata(domain_name)),
            xml=libvirt.VolumeXmlArgs(xslt=volume_xslt()),
            opts=self.child_opts(
                # First-boot input: Talos reads the seed's user-data only
                # while STATE holds no configuration, and the day-1 chain's
                # `ConfigurationApply` keeps the running worker current, so a
                # new rendering is no reason to touch the seed or the domain
                # that attaches it.
                ignore_changes=['source'],
                # And so it is replaced with the disk it boots: a replacement
                # is created from the program's current inputs, so a new disk
                # boots the rendering current at its run rather than the one
                # the seed was first created with.
                replace_with=[self.volume],
                # Its name is stated, for the reason the disk's is.
                delete_before_replace=True,
            ),
        )

        self.domain = libvirt.Domain(
            domain_name,
            name=domain_name,
            vcpu=vcpus,
            memory=memory_gib * MIB_PER_GIB,
            machine=MACHINE_TYPE,
            cpu=libvirt.DomainCpuArgs(mode=CPU_MODE),
            # Started with the host: a cluster node that does not come back
            # from a host reboot is a node somebody has to remember to start.
            autostart=True,
            # Running, and started again by the next apply that finds it
            # stopped: the day-1 chain waits on this node answering. It is
            # the provider's default, stated so the decision reads here.
            running=True,
            # virtio-blk: the provider's default bus, and `scsi` is left off
            # deliberately — a virtio-scsi controller would buy nothing that
            # either disk needs. The data disk first, so it is `vda`; the seed
            # second, which Talos finds by its label whatever its bus.
            disks=[
                libvirt.DomainDiskArgs(volume_id=self.volume.id),
                libvirt.DomainDiskArgs(volume_id=self.seed.id),
            ],
            # The second host bridge, which is the one over the cluster VLAN.
            # The existing bridge enslaves the IoT VLAN, which is where the
            # Home Assistant domain belongs and a cluster node does not; the
            # worker reaches its own subnet, its default route and its BGP
            # peer through this one.
            network_interfaces=[libvirt.DomainNetworkInterfaceArgs(bridge=bridge)],
            # The host is headless, so a serial console is how a machine that
            # fails before apid comes up says why.
            consoles=[libvirt.DomainConsoleArgs(type='pty', target_type='serial', target_port='0')],
            xml=libvirt.DomainXmlArgs(xslt=domain_xslt(disk_volume=disk_volume, seed_volume=seed_volume)),
            # A domain's name is fixed at definition time and this one is
            # stated, so a replacement — a RAM change, above all — has to
            # undefine the old domain before it defines the new one.
            opts=self.child_opts(delete_before_replace=True),
        )

        self.register_outputs({})


def _sole_worker(cluster: TalosCluster) -> str:
    """The one node this host runs.

    One big VM rather than several is a decision with a reason (nodes.md
    §4.3): on a single host more VMs buy no fault isolation and each costs a
    fixed kubelet-and-CNI overhead out of a memory budget that has none to
    spare. A second worker is deferred with criteria, so a cluster that names
    two workers is a design change and not something to spread over this host
    silently.
    """
    match cluster.worker_nodes:
        case (node,):
            return node
        case workers:
            raise ValueError(f'the homelab host runs exactly one worker VM, and the cluster names {list(workers)}')
