"""The homelab worker: one VM under libvirt on the home site's own host."""

from __future__ import annotations

from ipaddress import IPv4Address

#: A pure worker, because the control plane is cloud-side (nodes.md §4.2).
HOMELAB_NODE = 'worker'

#: Its address is a constant rather than a lease: the gateway's FRR names it
#: as a BGP neighbour, the peer-port forward sends traffic to it and day 1
#: dials apid at it, so it is configured statically in machine config on one
#: side and read from here on the others (physical/homelab-host.md §2).
#: Nodes number from `.10`; `.1` is the gateway's leg and `.2` the homelab
#: host's own leg on the bridge, which is host preparation rather than
#: anything this program declares.
HOMELAB_NODE_IPV4 = IPv4Address('192.168.70.10')

#: Bootstrap sizing, deliberately below the 12–16 vCPU / 20 GiB / 100+ GB end
#: state: the legacy cluster still holds that RAM and that disk, and the VM
#: grows one wave at a time as legacy workloads stop (migration.md §0.4).
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
#: decided where the session is dialled rather than carried around with the key.
HOMELAB_HOST_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIHV/ogdnUUf2j2DIffv86Ra43SS672UCZt3kXSvs6FF'
