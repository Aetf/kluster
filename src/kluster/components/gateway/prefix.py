"""The site's delegated prefix, as `Gateway` reads it: the one construction of the PD lease read.

The ISP decides the prefix and moves it when it likes, so the program reads it
on every run instead of holding it (style/pulumi.md, "Data"). The controller
reports only the per-network /64s, so what is read is the device's PD lease
(`kluster.providers.pd_lease`), and this module is the one place that builds
that read. What leaves it is the shape a controller field would give: the
delegated prefix with its length, and the /64s the site's networks hold. The
lease's own shape, keyed by bridge, stays inside the layer, which comes out
whole once a controller field carries the held prefix (the provider package
says how): the construction below then reads that field instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

import pulumi

from kluster.providers.device_files.provider import Connection
from kluster.providers.pd_lease import DelegatedPrefixLease

__all__ = ('WAN_INTERFACE', 'HeldPrefix', 'held_prefix')

#: The device's interface the ISP delegates the prefix on: the WAN every
#: network on the prefix takes its /64 from (`ipv6_pd_interface='wan'` on the
#: controller), as the device names it.
WAN_INTERFACE = 'eth8'


@final
@dataclass(frozen=True)
class HeldPrefix:
    """The read, and the two values it gives a consumer."""

    read: DelegatedPrefixLease
    #: The delegated prefix with its length.
    prefix: pulumi.Output[str]
    #: The /64s the site's networks hold on it, sorted.
    subnets: pulumi.Output[list[str]]


def held_prefix(name: str, *, connection: Connection, opts: pulumi.ResourceOptions) -> HeldPrefix:
    """Read the prefix delegated on `WAN_INTERFACE` off the device `connection` names, on every run."""
    read = DelegatedPrefixLease(name, connection=connection, wan=WAN_INTERFACE, opts=opts)
    return HeldPrefix(
        read=read,
        prefix=read.prefix,
        subnets=read.subnets.apply(lambda held: sorted(held.values())),
    )
