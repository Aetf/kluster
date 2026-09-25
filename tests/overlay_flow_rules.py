"""The overlay's rule program, rendered over addresses a suite can name.

`flow_rules` is a pure function of what it is handed, so the addresses here
are literals rather than the census's: a suite asserting what the program says
about them reads them from here, beside the call that hands them over.
"""

from __future__ import annotations

from ipaddress import IPv4Address

from kluster.components.overlay import flow_rules as rules_module

#: The gateway's overlay address, as a member of the network holds one.
GATEWAY_OVERLAY = IPv4Address('10.144.1.1')
#: The homelab host's overlay address: the libvirt session reaches it member to
#: member, needing no managed route.
HOMELAB_OVERLAY = IPv4Address('10.144.180.10')
#: The resolvers' container-VLAN addresses. They are named at their site
#: addresses because they are containers on the device rather than members of
#: the overlay, and a routed packet still carries the destination it had before
#: the forward.
RESOLVERS = (IPv4Address('10.0.5.11'), IPv4Address('10.0.5.12'))


def rules() -> str:
    return rules_module.flow_rules(
        gateway_overlay_address=GATEWAY_OVERLAY,
        homelab_overlay_address=HOMELAB_OVERLAY,
        resolver_site_addresses=RESOLVERS,
    )
