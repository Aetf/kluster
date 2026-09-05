"""The homelab side of the physical layer, under libvirt (physical.md §3).

One domain on this host is this program's: the Talos worker VM, with the
storage pool, disk and cloud-init seed it boots from. The home-automation
domain that shares the host is **not declared here** — it predates this
program, outlives it, and belongs to the host's own configuration management
(rfc-002 §13).

The worker's machine configuration reaches it on a cloud-init seed image
rather than through a metadata service: there is no cloud platform here to
serve one. That seed carries cluster secrets, so it lives beside the disk
image on the same root-only, snapshot-excluded subvolume.

**The connection is derived, and its credential is materialized** rather than
recorded: an SSH transport is authenticated by files on the machine running
the program, and their paths are a property of that machine rather than of the
site. `connection_uri` writes both into the checkout and returns the URI that
names them, so a workstation and a continuous-integration runner reach the
host the same way from the same configuration.

**The disk is created from the Talos image, not created empty.** The volume's
`source` is the decompressed `nocloud` artefact on the machine running the
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
    beside `source` and sets the volume's capacity from the image, which is
    the Talos artefact's own ~1.25 GB. Reaching the worker's working size is
    therefore the *first* use of that host-side step rather than a later one,
    and the file and the declaration part company from the moment it runs —
    which is why `size` is ignored here as well. Every field of a libvirt
    volume replaces the volume, so a program insisting on a size would propose
    destroying the worker's disk the first time a refresh read the grown file
    back.

The image is a creation-time fact for the same reason. Talos upgrades itself
in place over its machine API — the declared artefact is what the disk was
*written* with, not what is on it now — so the declaration stops describing the
volume the first time the node is upgraded, and a later `versions:talos` bump
must not propose rewriting a running node's disk. Rebuilding the worker from a
newer image is therefore a deliberate act rather than a diff: unprotect the
volume, replace it, protect it again, and let the day-1 chain bring the node
back. It destroys everything the node held, which is why nothing does it by
accident.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pulumi
import pulumi_libvirt as libvirt

from kluster import conventions
from kluster.components.talos import TalosCluster
from kluster.lib import templates, workstation
from putils import Component, own_provider_opts, with_provider

__all__ = ('LIBVIRT_USER', 'PRIVATE_KEY', 'HomelabHost', 'connection_uri', 'slot')

#: The package `importlib.resources` resolves this module's `templates/`
#: directory against, so the stylesheet travels with the code that reads it
#: (rfc-002 §9.1).
_TEMPLATE_PACKAGE = 'kluster.components.homelab'

#: Memory is quoted in GiB (nodes.md §4.2) and libvirt domains are sized in
#: MiB. Disk sizes have no counterpart here: a volume created from a source
#: image takes that image's size, and the rest is host-side growth.
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

#: The account the session authenticates as: a dedicated service user on the
#: host, in the `libvirt` group and no other, provisioned together with its key
#: by the host's own configuration management (homelab-host.md §4).
LIBVIRT_USER = 'virt'

#: The workstation slot the run materializes its transport into
#: (credentials.md §1 rule 6): the checkout's git-ignored `.credentials/`, one
#: directory deeper so this pair sits beside another consumer's rather than in
#: the root of it.
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

    Both files in it are **working files, not slots**: a slot is durable and
    written by a `credentials` command — a kit, a passphrase, a client
    bundle — while these two are derived from stack configuration by this
    component on every run, owned by it and disposable. They live under
    `.credentials/` anyway because that directory is the `0700` boundary, the
    one answer to "what on this machine is secret", and a second git-ignored
    directory would be a second boundary to get right. Nothing here is
    produced by a command, so nothing should go looking for the command that
    produced it.
    """
    return (workstation.directory() if root is None else root / workstation.DIRECTORY) / SLOT


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

    What the provider honours here is not libvirt's own remote driver: the
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


def disk_tuning_xslt() -> str:
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

    The stylesheet takes no parameters and is read verbatim: the disk format it
    names is `DISK_FORMAT`, which the volume is created with, and the suite
    holds the two in step.
    """
    return templates.load(_TEMPLATE_PACKAGE, 'templates/disk-tuning.xslt')


def seed_metadata(hostname: str) -> str:
    """The `meta-data` half of the nocloud seed.

    `instance-id` is what a nocloud datasource compares to decide whether a
    boot is a *first* boot, so it is derived from the node rather than
    generated: a fresh value on every apply would re-run first-boot logic on a
    machine that has been running for months. Emitted as JSON because JSON is
    YAML, and the program then needs no serializer to state two facts.
    """
    return json.dumps({'instance-id': hostname, 'local-hostname': hostname})


class HomelabHost(Component, pulumi_type='kluster:physical:HomelabHost'):
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
        the seed. The pool that points at it is declared here; the subvolume
        and its `chattr +C` are host preparation (homelab-host.md §4).
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

        self.volume = libvirt.Volume(
            f'{domain_name}-disk',
            name=f'{domain_name}.{DISK_FORMAT}',
            pool=self.pool.name,
            format=DISK_FORMAT,
            # The bytes the worker boots. No `size` beside it: the provider
            # refuses the pair and takes the volume's capacity from the image.
            source=image_path,
            opts=self.child_opts(
                protect=True,
                # Both are facts about the volume's *creation*, and every field
                # of a libvirt volume replaces the volume, so neither may
                # become a diff afterwards:
                #
                # -   `size` is the image's at creation and the host's from the
                #     first `truncate` onwards.
                # -   `source` describes the bytes the disk was written with,
                #     and stops describing what is on it the moment Talos
                #     upgrades itself over the machine API. Insisting on it
                #     would turn a routine `versions:talos` bump — which this
                #     stack makes for the machine configuration anyway — into a
                #     proposal to rewrite a running node's disk, which `protect`
                #     would then refuse for as long as the bump stood.
                ignore_changes=['size', 'source'],
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
            opts=self.child_opts(),
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
            # The second host bridge, which is the one over the cluster VLAN.
            # The existing bridge enslaves the IoT VLAN, which is where the
            # Home Assistant domain belongs and a cluster node does not; the
            # worker reaches its own subnet, its default route and its BGP
            # peer through this one.
            network_interfaces=[libvirt.DomainNetworkInterfaceArgs(bridge=bridge)],
            # The host is headless, so a serial console is how a machine that
            # fails before apid comes up says why.
            consoles=[libvirt.DomainConsoleArgs(type='pty', target_type='serial', target_port='0')],
            xml=libvirt.DomainXmlArgs(xslt=disk_tuning_xslt()),
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
