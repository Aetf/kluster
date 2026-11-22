"""A `zerotier-cli peers` listing with a physical path in every form, a finder for any address left in a text, and stand-ins for the daemon.

Shared by the cases that hold the filter and the reading themselves
(`test_zt_reading`) and the ones that hold the join action's listing to go
through the filter and its jobs to take the reading again
(`test_zerotier_action`).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
from collections.abc import Mapping
from pathlib import Path

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


#: The interface the stand-in daemon names for the network.
DEVICE = 'ztprobe0'


def zerotier_stand_ins(
    directory: Path, *, peers: list[dict[str, object]], network: Mapping[str, object], mtu: int | None
) -> Path:
    """A directory of stand-ins for `sudo zerotier-cli -j` and `ip -j link`, answering as the daemon and the kernel do.

    `peers` is the `peers` listing and `network` the one network `listnetworks`
    lists, whose interface is `DEVICE` with an MTU of `mtu`, or one `ip` does not
    report with `None`. Anything else
    either is asked fails, as a command it does not know would.
    """
    answers = directory / 'answers'
    answers.mkdir()
    (answers / 'peers').write_text(json.dumps(peers))
    (answers / 'listnetworks').write_text(json.dumps([{'portDeviceName': DEVICE, **network}]))
    (answers / 'link').write_text(json.dumps([{'ifname': DEVICE} | ({} if mtu is None else {'mtu': mtu})]))
    bin_dir = directory / 'bin'
    bin_dir.mkdir()
    for name, script in {
        'sudo': f'#!/bin/sh\n[ "$1 $2" = "zerotier-cli -j" ] && exec cat {answers}/"$3"\nexit 64\n',
        'ip': f'#!/bin/sh\n[ "$*" = "-j link show dev {DEVICE}" ] && exec cat {answers}/link\nexit 64\n',
    }.items():
        (bin_dir / name).write_text(script)
        (bin_dir / name).chmod(0o755)
    return bin_dir


def peer(node: str, role: str, latency: int, address: str | None, *, tunneled: bool = False) -> dict[str, object]:
    """One peer as `zerotier-cli -j peers` prints it, with its preferred path at `address`, or none."""
    paths = [] if address is None else [{'address': address, 'active': True, 'expired': False, 'preferred': True}]
    return {'address': node, 'role': role, 'latency': latency, 'paths': paths, 'tunneled': tunneled}
