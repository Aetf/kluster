"""The site's delegated prefix, as `Gateway` reads it: the one construction of the PD lease read.

The ISP decides the prefix and moves it when it likes, and the firmware numbers
each network's /64 of it itself, so the program reads both on every run
instead of holding them (style/pulumi.md, "Data"). The controller reports
nothing that carries the prefix's length, so what is read is the device's PD
lease (`kluster.providers.pd_lease`), and this module is the one place that
builds that read. What leaves it is the shape a controller field would give:
the delegated prefix with its length, the /64s the site's networks hold, and
the /64 each declared network holds. The lease's own shape, a line per bridge,
stays inside the layer, which comes out whole once a controller field carries
the held prefix (the provider package says how): the construction below then
reads that field instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv6Network
from typing import final

import pulumi

from kluster import conventions
from kluster.providers.device_files.provider import Connection
from kluster.providers.pd_lease import LEASES, DelegatedPrefixLease

__all__ = ('WAN_INTERFACE', 'HeldPrefix', 'SubnetNotHeld', 'held_prefix', 'network_subnets')

#: The device's interface the ISP delegates the prefix on: the WAN every
#: network on the prefix takes its /64 from (`ipv6_pd_interface='wan'` on the
#: controller), as the device names it.
WAN_INTERFACE = 'eth8'


class SubnetNotHeld(ValueError):
    """The read names no /64 on the bridge a declared network is carried on."""


def network_subnets(held: Mapping[str, str], bridges: Mapping[conventions.SiteNetwork, str]) -> dict[str, IPv6Network]:
    """The /64 each network of `bridges` holds, by its bridge: the read's line for the bridge, or a refusal naming both.

    A declared network the read gives no /64 holds no address this program
    could name on it, so the run stops at the read rather than deriving one.
    """
    subnets: dict[str, IPv6Network] = {}
    for network, bridge in bridges.items():
        if bridge not in held:
            raise SubnetNotHeld(
                f'{LEASES} names no /64 on {bridge} for {WAN_INTERFACE}, '
                f'and {bridge} is the bridge the {network.name} network is carried on'
            )
        subnets[bridge] = IPv6Network(held[bridge])
    return subnets


@final
@dataclass(frozen=True)
class HeldPrefix:
    """The read, and the values it gives a consumer."""

    read: DelegatedPrefixLease
    #: The delegated prefix with its length.
    prefix: pulumi.Output[str]
    #: The /64s the site's networks hold on it, sorted.
    subnets: pulumi.Output[list[str]]
    #: The /64 each declared network holds, by the bridge it is carried on
    #: (`network_subnets`).
    declared: pulumi.Output[dict[str, IPv6Network]]


def held_prefix(
    name: str,
    *,
    connection: Connection,
    bridges: Mapping[conventions.SiteNetwork, str],
    opts: pulumi.ResourceOptions,
) -> HeldPrefix:
    """Read the prefix delegated on `WAN_INTERFACE` off the device `connection` names, on every run.

    `bridges` are the networks this program declares on the prefix and the
    bridge the device carries each on, whose /64s the read is held to give.
    """
    read = DelegatedPrefixLease(name, connection=connection, wan=WAN_INTERFACE, opts=opts)
    return HeldPrefix(
        read=read,
        prefix=read.prefix,
        subnets=read.subnets.apply(lambda held: sorted(held.values())),
        declared=read.subnets.apply(lambda held: network_subnets(held, bridges)),
    )
