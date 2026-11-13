"""The homelab worker: one VM under libvirt on the home site's own host."""

from __future__ import annotations

from ipaddress import IPv4Address

from kluster.conventions.site import eui64_link_local

#: A pure worker, because the control plane is cloud-side (nodes.md §4.2).
HOMELAB_NODE = 'worker'

#: Its address is a constant rather than a lease: the gateway's FRR names it
#: as a BGP neighbor, the peer-port forward sends traffic to it and day 1
#: dials apid at it, so it is configured statically in machine config on one
#: side and read from here on the others (physical/homelab-host.md §2).
#: Nodes number from `.10`; `.1` is the gateway's leg and `HOMELAB_HOST_LEG_IPV4`
#: the homelab host's own.
HOMELAB_NODE_IPV4 = IPv4Address('192.168.70.10')

#: The MAC the worker's one interface is declared with. A convention rather
#: than the provider's choice because an address is derived from it: the kernel
#: forms the worker's link-local on the uplink from this MAC, and the gateway
#: routes the `lan` pool's IPv6 VIPs through that link-local. A generated MAC
#: changes with every replacement of the domain, and the gateway's routes would
#: then point at an address nothing holds, with nothing reporting it. In QEMU's
#: locally administered range, and distinct from every other guest's on the
#: host (physical/homelab-host.md §2).
#:
#: **Changing it once the domain exists is a migration.** The gateway's next
#: hop is derived from it and moves on the next `up`, while the running domain
#: keeps the MAC it was created with: the provider applies this value when it
#: creates the domain and never in place. So the change lands only when the
#: domain is replaced, in the same `up` or a later one; until then the IPv6
#: VIPs go nowhere, and the only sign is a domain diff that never converges.
HOMELAB_NODE_MAC = '52:54:00:07:00:0a'

#: The worker's link-local address on the cluster VLAN, derived from its MAC
#: rather than typed beside it, so the two cannot disagree: the next hop the
#: gateway's routing configuration sets for every IPv6 route the worker
#: announces (`components.gateway.routing`).
HOMELAB_NODE_LINK_LOCAL = eui64_link_local(HOMELAB_NODE_MAC)

#: The homelab host's own leg on the bridge, on the cluster VLAN: host
#: preparation rather than anything this program declares, and the address the
#: worker reaches the NAS at (physical/homelab-host.md §2, §5).
HOMELAB_HOST_LEG_IPV4 = IPv4Address('192.168.70.2')

#: The one NFS export the host serves the worker, whole: a single filesystem
#: with no dataset boundary under it, so a NAS claim names its path under this
#: root and no export changes for a new path (physical/homelab-host.md §5). It
#: squashes every client to its anonymous user, which is who a pod writes as.
NAS_EXPORT = '/mnt/nas'

#: Bootstrap sizing, deliberately below the end state of 12–16 vCPU, 100+ GB
#: of disk and the RAM the decommission decides (nodes.md §4.2): the legacy
#: cluster still holds that RAM and that disk, so the RAM grows in one drained
#: window, Wave C's, and the disk as legacy data is deleted (migration.md §0.4).
#: Growing *these two* is an edit here and a previewed apply: `vcpu` and
#: `memory` replace the domain, which is a stop, an undefine, a define and a
#: start with the disk — a separate resource — surviving, so the cost is a
#: drained window rather than a rebuild.
#:
#: The disk is not among them and has no constant here. A libvirt volume has
#: no update path — every field replaces it, `size` included — so the
#: declaration states no size at all and ignores the one it reads back; the
#: disk grows on the host instead, `truncate` plus `virsh blockresize`
#: (physical/homelab-host.md §1).
HOMELAB_VCPUS = 12
HOMELAB_MEMORY_GIB = 10

#: The host bridge the worker's tap joins, which bridges the cluster VLAN's
#: tagged subinterface. A second bridge on purpose: the existing `kvmbr0`
#: enslaves the IoT VLAN, which is where the Home Assistant domain belongs and
#: where a cluster node does not (physical/homelab-host.md §2).
HOMELAB_BRIDGE = 'kvmbr1'

#: The directory the libvirt pool points at, holding the worker's disk image
#: and its seed. A convention rather than a setting because both sides have to
#: name the same path: this program declares the pool, and the host's own
#: configuration management creates the nodatacow subvolume under it
#: (physical/homelab-host.md §4). An operator who changed one alone would have
#: a pool over a directory nobody prepared.
HOMELAB_STORAGE_DIR = '/var/lib/libvirt/kluster'

#: The host's SSH host key, pinned. It is code rather than configuration for
#: two reasons: a public key is not a secret, and a pin typed in beside the
#: client credential could be replaced by whoever could already replace the
#: credential. Stored in the installation's `authorized_keys` form — the bare
#: `ssh-ed25519 AAAA…` blob, no host name in front of it
#: (`providers/device_files/ssh.py`) — so the address it is written against is
#: decided where the session is dialed rather than carried around with the key.
HOMELAB_HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIHV/ogdnUUf2j2DIffv86Ra43SS672UCZt3kXSvs6FF'
