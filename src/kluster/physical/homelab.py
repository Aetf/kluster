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

Not implemented: `declare` raises, so a run that reaches this domain says so
instead of quietly leaving the cluster one node short.
"""

from __future__ import annotations

import pulumi

from kluster.physical.talos import TalosCluster

__all__ = ('declare',)


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
    raise NotImplementedError(
        'physical §3 homelab: the libvirt worker VM and the adopted Home Assistant domain '
        'are not declared yet — see docs/declarative/physical.md §3 and '
        'docs/physical/homelab-host.md'
    )
