"""The hosts one client may not resolve, with the client the instances hold for it.

Data in the `dns` program's own area: the client as `AdGuardClients` takes it,
and its hosts, one per line in `ps4-hosts.txt` beside this module, because a
list that long is data rather than code (style/python.md). The client set both
instances hold is drawn from these rows (`rewrites.blocklist_clients`), so a
rule names no client an instance lacks.

The console is identified by its two MAC addresses, which an instance learns
only for a client on its own segment or one it leases to; whether the
instances identify it at all is `kluster-ops#514`'s question.
"""

from __future__ import annotations

from kluster.components.dns.rewrites import Blocklist
from kluster.lib import templates
from kluster.providers.adguard import Client

__all__ = ('PS4', 'PS4_UPDATES', 'PS4_UPDATES_SOURCE')

_TEMPLATE_PACKAGE = 'kluster.components.dns'

#: A game console on the global settings, which only the rules below single out.
PS4: Client = {
    'name': 'PS4',
    'ids': ['f8:46:1c:59:a0:8f', 'f8:da:0c:97:94:ad'],
    'tags': ['device_gameconsole'],
    'upstreams': [],
    'use_global_settings': True,
    'filtering_enabled': False,
    'parental_enabled': False,
    'safebrowsing_enabled': False,
    'safe_search': {
        'enabled': False,
        'bing': True,
        'duckduckgo': True,
        'ecosia': True,
        'google': True,
        'pixabay': True,
        'yandex': True,
        'youtube': True,
    },
    'use_global_blocked_services': True,
    'blocked_services': [],
    'blocked_services_schedule': {'time_zone': 'Local'},
    'ignore_querylog': False,
    'ignore_statistics': False,
    'upstreams_cache_enabled': False,
    'upstreams_cache_size': 0,
}

#: Where the host list comes from, as the comment above its rules names it.
PS4_UPDATES_SOURCE = (
    "the PS4's system-update and network hosts, for that console alone (github.com/Misl3d/PS-dns-block)"
)

#: The hosts the console may not resolve, in the order the list keeps them.
PS4_UPDATES = Blocklist(client=PS4, hosts=tuple(templates.load(_TEMPLATE_PACKAGE, 'ps4-hosts.txt').split()))
