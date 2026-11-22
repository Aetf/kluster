"""A `zerotier-cli peers` listing with a physical path in every form, and a finder for any address left in a text.

Shared by the cases that hold the filter itself (`test_zt_reading`) and the
one that holds the join action's listing to go through it
(`test_zerotier_action`).
"""

from __future__ import annotations

import contextlib
import ipaddress
import re

#: A `zerotier-cli peers` listing as the daemon prints it, with a physical
#: path in each form a path is written in: ZeroTier's own `<address>/<port>`
#: for IPv4, compressed IPv6, full IPv6 and IPv4-mapped IPv6, and the
#: `[<IPv6>]:<port>` and `<IPv4>:<port>` forms other tools write. The
#: addresses are documentation ranges (RFC 5737, RFC 3849).
PEERS = (
    '200 peers\n'
    '<ztaddr>   <ver>  <role> <lat> <link>   <lastTX> <lastRX> <path>\n'
    '778cde7190 -      PLANET    84 DIRECT   1630     1546     192.0.2.66/9993\n'
    'cafe04eba9 -      PLANET   160 DIRECT   1630     1470     2001:db8:d405::1/9994\n'
    '62f865ae71 -      PLANET   201 DIRECT   1630     1490     2001:0db8:0000:0000:0000:0000:0000:0002/9995\n'
    '537a9626fa 1.14.2 LEAF      23 DIRECT   1630     1630     ::ffff:198.51.100.7/21032\n'
    'c3755c24d1 1.14.2 LEAF      31 RELAY    1630     1630     [2001:db8::7]:21033\n'
    'fb6c235c67 1.14.2 LEAF      44 DIRECT   1630     1630     203.0.113.9:21034\n'
    'abcdef0123 1.16.2 LEAF      -1 RELAY\n'
)
#: The ports in `PEERS`, none of which may survive the filter.
PEER_PORTS = ('9993', '9994', '9995', '21032', '21033', '21034')


def addresses(text: str) -> list[str]:
    """Every IP address in `text`, found by parsing rather than by the filter's own pattern."""
    found: list[str] = []
    for run in re.findall(r'[0-9A-Fa-f:.\[\]]+', text):
        for piece in run.strip('[]').split('/'):
            for candidate in (piece.strip('[]'), piece.rsplit(':', 1)[0].strip('[]')):
                with contextlib.suppress(ValueError):
                    found.append(str(ipaddress.ip_address(candidate)))
                    break
    return found
