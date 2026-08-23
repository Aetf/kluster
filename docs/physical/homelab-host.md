# Physical Design: the Homelab Host & Worker VM

The system design of the homelab side's physical layer: how the worker
VM is shaped on the host (disk, network, GPU), and what the host
itself must provide. Sizing and the host inventory live in
[cluster/nodes.md](../cluster/nodes.md) §4; how these resources are
*declared* (pulumi-libvirt mechanics, HAOS import) lives in
[declarative/physical.md](../declarative/physical.md) §3.

> **Status**: designed 2026-08-23 (as declarative/physical.md §3),
> extracted to this topic 2026-08-24. Not implemented.

## 1. VM disk: raw sparse file on the root btrfs, nodatacow, virtio-blk

The NVMe is a single btrfs partition (no LVM, no spare partitions), so
a file it is — the shape makes that rational:

-   **Dedicated subvolume with `chattr +C`** (nodatacow, set on the
    directory before image creation). VM images are the canonical
    CoW-on-CoW pathology — the guest's random small writes fragment a
    checksummed CoW file without bound. nodatacow trades btrfs
    checksums/compression for sane write behavior; integrity of the
    data that matters is owned by the *cluster's* backup regime, not
    the host fs. The subvolume boundary also keeps the image out of
    any host snapshot/send scope.
-   **Raw, not qcow2**: with CoW disabled, qcow2's allocation layer
    buys nothing but indirection. Growth (60 → 100+ GB interleaved
    with reclamation, migration.md §0.4) is `truncate` on the file +
    `virsh blockresize`; Talos grows its EPHEMERAL partition into the
    new space on its own.
-   **virtio-blk with `discard=unmap`, `cache=none`**: in-guest TRIM
    punches holes back out of the sparse file, so NVMe space actually
    returns to the host when the guest deletes data — load-bearing
    for the interleaved migration. `cache=none` avoids double-caching
    guest I/O through the host page cache.
-   **What btrfs still contributes**: an offline `cp --reflink` of
    the image is a free instant copy before risky day-2 surgery
    (Talos upgrades are already A/B; this is belt-and-suspenders).
    Offline only — reflinking a running nodatacow image yields an
    inconsistent copy.

## 2. VM network: a second host bridge, HAOS-pattern but not the HAOS bridge

The *mechanism* is copied from the HAOS domain exactly — a
systemd-networkd Linux bridge + virtio NIC tap — but the existing
`kvmbr0` enslaves the **IoT VLAN** (192.168.90.0/24), which is where
HAOS belongs with its devices and where the worker VM does not. The
worker joins the host's untagged network — the **default LAN, br0 on
the UDM** (192.168.80.0/24; the host, NAS serving, and the UDM BGP
session all live there) — which today has **no bridge** on the host:
`enp7s0` carries the host address directly. So the host network config
(aconfmgr-managed systemd-networkd) gains a second bridge (say
`kvmbr1`) enslaving `enp7s0`, with the host's address/DHCP moving onto
the bridge — one brief connectivity blip, done in the same aconfmgr
change-set that installs the libvirt resources (§4).

A real bridge, **not macvtap**: macvtap trades away host↔guest
connectivity for zero host-network reconfiguration (frames from the
host's own IP on the parent NIC can't hairpin back to a macvtap VM
through an ordinary switch), and host↔VM is load-bearing here — NFS
from the NAS role into the cluster, talosctl/management sessions from
the host. Off-host traffic (the UDM's FRR session, LAN clients) would
be fine either way; it is specifically the host side that macvtap
severs. HAOS itself runs tap-on-bridge today (verified 2026-08-23:
`vnet0` is a tap slaved to `kvmbr0`), so "copy the HAOS mechanism" and
"real bridge" are the same statement.

Addressing: **static IPv4 in the Talos machine config** (the UDM's FRR
neighbor address must not depend on a DHCP lease) + SLAAC GUA/ULA for
v6 (architecture.md §3.5's qbittorrent path expects the VM's SLAAC
GUA).

## 3. GPU: two-phase VFIO passthrough

The VM bootstraps with no hostdev (host i915 keeps serving the legacy
cluster), but its Talos schematic carries the i915 firmware extension
from day 0 (harmless without a GPU) so the later cutover touches no OS
image. The cutover itself — drain → host binds vfio-pci → domain gains
the PCI hostdev → reboot → device plugin sees the GPU — is a migration
window sequenced with the immich/jellyfin move (migration.md Wave C).
The capability is verified early on a scratch VM (bootstrap
verification, declarative/physical.md §6).

## 4. Host preparation: one aconfmgr change-set

A prerequisite the Pulumi program assumes rather than manages (the
host is not a Pulumi target; the same boundary as the NAS role). Its
contents, so nothing is discovered mid-bootstrap:

-   the second bridge with the host address moved onto it (§2);
-   the nodatacow subvolume (§1) + a libvirt storage pool pointing at
    it;
-   an SSH identity for the libvirt provider (`qemu+ssh://` over the
    CI ZeroTier join — a user in the `libvirt` group, no root; note
    that `libvirt`-group access is root-*equivalent* in effect —
    domain XML can map any host device or disk — so this identity is
    guarded at the same tier as the UDM key, not as an unprivileged
    account);
-   the NAS NFS exports extended to the worker VM's static IP.

The vfio-pci host binding is deliberately *not* here — it lands in the
Wave C cutover (§3, migration.md).
