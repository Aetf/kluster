"""A mock monitor standing in for the gateway's controller, for the suites that declare its firewall.

The firewall looks up the controller's two stock zones by name rather than
creating them, so a run under mocks needs something to answer that lookup. The
answer is an invention: every zone exists, is empty, and has the id `zone_id`
spells for it. A suite asserting which zone a rule names compares against
`zone_id` rather than against a spelling of its own.

Every suite whose run reaches the firewall shares this module rather than
carrying a copy -- its own suites and the whole physical program's -- because a
copy is a second place to change when the lookup does, and the copy nobody
updates answers a question the program no longer asks.
"""

from __future__ import annotations

from typing import Any, cast, override

import pulumi
from mock_monitor import Recorder

#: The invoke the firewall resolves a stock zone through.
ZONE_LOOKUP = 'unifi:index/getFirewallZone:getFirewallZone'


def zone_id(name: str) -> str:
    """The id `Controller` answers for the zone of this name."""
    return f'zone-{name}'


class Controller(Recorder):
    """A monitor that answers the firewall's zone lookups; every zone exists and is empty.

    The zone is reported on the site the lookup asked about. A lookup that
    names none gets `site`, standing in for the site the real provider is
    configured with, which is what it falls back to.
    """

    def __init__(self, site: str) -> None:
        super().__init__()
        self.site: str = site

    @override
    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == ZONE_LOOKUP:
            asked = cast('dict[str, Any]', args.args)
            name = str(asked['name'])
            return {'id': zone_id(name), 'name': name, 'networks': [], 'site': asked.get('site', self.site)}
        return {}
