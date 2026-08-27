"""The homelab side of the physical layer, under libvirt (physical.md §3).

Two domains on one host, sharing one provider connection: the Talos worker VM
this program creates, and the Home Assistant domain it *adopts*. The second is
the interesting one — the domain predates the program, carries the home's
automation, and is imported by UUID rather than rebuilt, because nothing about
it may be recreated. Its disks and passthrough devices are its identity; the
domain XML is only metadata.

The worker's machine configuration reaches it on a cloud-init seed image
rather than through a metadata service: there is no cloud platform here to
serve one. That seed carries cluster secrets, so it lives beside the disk
image on the same root-only, snapshot-excluded subvolume.

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
    The declared size is therefore the size the image is *created* at and is
    ignored afterwards: a program insisting on it would propose replacing the
    worker's disk the first time a refresh read the grown file back.

**Not declared here yet**: the Talos image itself. The volume is created
empty, and nothing in the program writes the `nocloud` disk image onto it —
the worker's schematic (amd64, `nocloud`, the i915 extension that the Wave C
GPU cutover needs present from day 0) has no declaration, and this function is
handed no image to install. Until that seam exists the VM is declared,
adopted and previewable but not bootable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pulumi
import pulumi_libvirt as libvirt

from kluster.physical.talos import TalosCluster
from putils import Component

__all__ = ('HomelabHost', 'declare')

#: Disk sizes are quoted in GB (nodes.md §4.2) and libvirt volumes are sized
#: in bytes; memory is quoted in GiB and libvirt domains are sized in MiB.
BYTES_PER_GB = 1000**3
MIB_PER_GIB = 1024

#: Raw, not qcow2: the image lives on a nodatacow subvolume, where qcow2's
#: allocation layer buys indirection and nothing else (homelab-host.md §1).
#: The volume is created sparse — the provider declares a capacity and no
#: allocation — so the file occupies what the guest has written.
DISK_FORMAT = 'raw'

#: Q35 rather than the provider's default i440fx. The machine type is fixed
#: at definition time, and the Wave C cutover hands this domain a PCI hostdev
#: (homelab-host.md §3) — PCIe, which i440fx does not have.
MACHINE_TYPE = 'q35'

#: The host CPU as it is. The domain has no migration target — there is one
#: host — so hiding the CPU's features behind a portable model would cost the
#: guest instruction sets and buy nothing.
CPU_MODE = 'host-passthrough'

#: Every input the libvirt provider accepts for a domain. The adopted Home
#: Assistant domain ignores all of them: Pulumi owns *that* it exists, the
#: host owns what it is. Written out rather than wildcarded so that a provider
#: that grows a field fails a test here instead of silently proposing a change
#: to a domain that carries the home's automation.
HOST_OWNED: tuple[str, ...] = (
    'arch',
    'autostart',
    'bootDevices',
    'cloudinit',
    'cmdlines',
    'consoles',
    'coreosIgnition',
    'cpu',
    'description',
    'disks',
    'emulator',
    'filesystems',
    'firmware',
    'fwCfgName',
    'graphics',
    'initrd',
    'kernel',
    'machine',
    'memory',
    'metadata',
    'name',
    'networkInterfaces',
    'nvram',
    'qemuAgent',
    'running',
    'tpm',
    'type',
    'vcpu',
    'video',
    'xml',
)


def disk_tuning_xslt(disk_format: str = DISK_FORMAT) -> str:
    """The stylesheet that gives the data disk `cache=none` and `discard=unmap`.

    Neither is expressible through the provider's disk block, and both are
    load-bearing (homelab-host.md §1): `discard=unmap` is what lets in-guest
    TRIM punch holes back out of the sparse image, so deleting data in the
    cluster returns NVMe to the host during the interleaved migration, and
    `cache=none` keeps guest I/O from being cached a second time in the host's
    page cache. The provider's escape hatch for exactly this is an XSLT
    transform of the domain XML it is about to define.

    The driver element is written rather than patched, so the result does not
    depend on which attributes the provider happened to emit. Only the data
    disk is matched: the seed is a cdrom, and it wants neither setting.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="xml" encoding="UTF-8" omit-xml-declaration="yes"/>
  <xsl:template match="node()|@*">
    <xsl:copy><xsl:apply-templates select="node()|@*"/></xsl:copy>
  </xsl:template>
  <xsl:template match="/domain/devices/disk[@device='disk']">
    <xsl:copy>
      <xsl:apply-templates select="@*"/>
      <driver name="qemu" type="{disk_format}" cache="none" discard="unmap"/>
      <xsl:apply-templates select="node()[not(self::driver)]"/>
    </xsl:copy>
  </xsl:template>
</xsl:stylesheet>
"""


def seed_metadata(hostname: str) -> str:
    """The `meta-data` half of the nocloud seed.

    `instance-id` is what a nocloud datasource compares to decide whether a
    boot is a *first* boot, so it is derived from the node rather than
    generated: a fresh value on every apply would re-run first-boot logic on a
    machine that has been running for months. Emitted as JSON because JSON is
    YAML, and the program then needs no serializer to state two facts.
    """
    return json.dumps({'instance-id': hostname, 'local-hostname': hostname})


def import_id(value: pulumi.Input[str]) -> str:
    """The domain UUID to adopt, in the form libvirt hands it back.

    An import id has to be known while the program is being *constructed* —
    it is a resource option, not an input — so a UUID that only arrives as an
    output is refused rather than silently ignored. The canonical form is used
    because the id Pulumi records is the one the provider reads back, and a
    UUID typed in upper case would otherwise never match it.
    """
    if not isinstance(value, str):
        raise ValueError('the Home Assistant domain UUID must be a plain string, known before the run')
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError(f'{value!r} is not a domain UUID: libvirt imports domains by UUID, not by name') from error


class HomelabHost(Component, pulumi_type='kluster:physical:HomelabHost'):
    """The worker VM, and the Home Assistant domain adopted beside it.

    :param cluster: the day-0 chain. The worker's configuration and the
        secrets the seed carries come out of the same place, so the component
        takes the cluster whole rather than a rendered string.
    :param connection_uri: the libvirt endpoint on the host, an SSH transport
        reached over the overlay.
    :param storage_dir: the nodatacow subvolume that holds the disk image and
        the seed. The pool that points at it is declared here; the subvolume
        and its `chattr +C` are host preparation (homelab-host.md §4).
    :param haos_domain_uuid: the domain to adopt.
    """

    def __init__(
        self,
        name: str,
        *,
        cluster: TalosCluster,
        connection_uri: pulumi.Input[str],
        storage_dir: pulumi.Input[str],
        bridge: pulumi.Input[str],
        vcpus: int,
        memory_gib: int,
        disk_gb: int,
        haos_domain_uuid: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        node = _sole_worker(cluster)
        domain_name = f'{name}-{node}'

        # One connection for both domains: they are two definitions on one
        # host, and the credential that reaches them is the same.
        self.provider = libvirt.Provider(f'{name}-libvirt', uri=connection_uri, opts=self.child_opts())

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
            opts=self._opts(protect=True),
        )

        self.volume = libvirt.Volume(
            f'{domain_name}-disk',
            name=f'{domain_name}.{DISK_FORMAT}',
            pool=self.pool.name,
            format=DISK_FORMAT,
            size=disk_gb * BYTES_PER_GB,
            opts=self._opts(
                protect=True,
                # The declared size is the creation size. Growth happens on
                # the host, and a refresh that read the grown file back would
                # otherwise diff against this number — and every field of a
                # libvirt volume, `size` included, replaces the volume.
                ignore_changes=['size'],
            ),
        )

        # Talos' nocloud platform reads its machine configuration from the
        # seed's user-data, which is why the worker needs no metadata service.
        self.seed = libvirt.CloudInitDisk(
            f'{domain_name}-seed',
            name=f'{domain_name}-seed.iso',
            pool=self.pool.name,
            user_data=cluster.machine_configs[node],
            meta_data=seed_metadata(domain_name),
            opts=self._opts(),
        )

        self.domain = libvirt.Domain(
            domain_name,
            name=domain_name,
            vcpu=vcpus,
            memory=memory_gib * MIB_PER_GIB,
            machine=MACHINE_TYPE,
            cpu=libvirt.DomainCpuArgs(mode=CPU_MODE),
            # A cluster node that does not come back from a host reboot is a
            # node somebody has to remember to start.
            autostart=True,
            running=True,
            # virtio-blk: the provider's default bus, and `scsi` is left off
            # deliberately — a virtio-scsi controller would buy nothing that
            # this disk needs.
            disks=[libvirt.DomainDiskArgs(volume_id=self.volume.id)],
            cloudinit=self.seed.id,
            # The second host bridge, on the untagged server LAN. The existing
            # bridge enslaves the IoT VLAN, which is where the Home Assistant
            # domain belongs and a cluster node does not.
            network_interfaces=[libvirt.DomainNetworkInterfaceArgs(bridge=bridge)],
            # The host is headless, so a serial console is how a machine that
            # fails before apid comes up says why.
            consoles=[libvirt.DomainConsoleArgs(type='pty', target_type='serial', target_port='0')],
            xml=libvirt.DomainXmlArgs(xslt=disk_tuning_xslt()),
            # A domain's name is fixed at definition time and this one is
            # stated, so a replacement — a RAM change, above all — has to
            # undefine the old domain before it defines the new one.
            opts=self._opts(delete_before_replace=True),
        )

        # Adoption, not creation (architecture.md §6.8). The import option is
        # what makes the program valid *before* the domain is in state: a
        # preview of a stack that has never run proposes importing this
        # domain rather than creating a second one, and once it is in state
        # the option is inert. Everything else about it is the host's, so the
        # first apply has nothing to change and `protect` turns any diff that
        # would replace it — the one outcome that must never happen — into a
        # refusal instead of an outage. The domain's name is not stated for
        # the same reason as the rest: the provider auto-names a domain it is
        # not given a name for, and on an import that name comes from the
        # domain that was read rather than from this program.
        self.haos = libvirt.Domain(
            f'{name}-haos',
            opts=self._opts(
                protect=True,
                import_=import_id(haos_domain_uuid),
                ignore_changes=list(HOST_OWNED),
            ),
        )

        self.register_outputs({})

    def _opts(self, **kwargs: Any) -> pulumi.ResourceOptions:
        """Child options that also carry the host's libvirt connection."""
        return self.child_opts(provider=self.provider, **kwargs)


def declare(
    name: str,
    *,
    cluster: TalosCluster,
    connection_uri: str,
    storage_dir: str,
    bridge: str,
    vcpus: int,
    memory_gib: int,
    disk_gb: int,
    haos_domain_uuid: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Declare the worker VM and adopt the Home Assistant domain.

    `connection_uri` is the libvirt endpoint on the host (an SSH transport
    reached over ZeroTier), `storage_dir` the nodatacow subvolume that holds
    both the raw disk image and the seed, and `haos_domain_uuid` identifies
    the domain to adopt — libvirt imports domains by UUID, and the UUID is the
    one attribute of that domain nothing may change.

    The Talos component comes in whole rather than as a rendered string: a
    worker's configuration and the secrets the seed must carry both come out
    of the same chain.
    """
    _ = HomelabHost(
        name,
        cluster=cluster,
        connection_uri=connection_uri,
        storage_dir=storage_dir,
        bridge=bridge,
        vcpus=vcpus,
        memory_gib=memory_gib,
        disk_gb=disk_gb,
        haos_domain_uuid=haos_domain_uuid,
        opts=opts,
    )


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
