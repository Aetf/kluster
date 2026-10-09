"""What both resolvers are configured with beyond the rule list, and the one function that assembles the whole.

Data in the `dns` program's own area, since no other program reads it, in the
administration API's own names (`kluster.providers.adguard`), so a value here
reads the way the instance reports it. Each value is the one the instances
answer with, apart from what the rendering of the rule list and the client set
derive.

**Two values are the program's to hand in, not this module's to know**: the
zones the instances forward to the gateway's own resolver, and that resolver's
address. Both are also read by `physical`, which renders them into the
instances' initial state, so they arrive as `configuration`'s parameters.
"""

from __future__ import annotations

from collections.abc import Sequence
from ipaddress import IPv4Address

from kluster import conventions
from kluster.components.dns.resolver import AdGuardConfiguration
from kluster.components.dns.rewrites import RuleBlock, blocklist_clients, user_rules
from kluster.providers.adguard import (
    AccessLists,
    BlockedServices,
    DnsSettings,
    FilteringSwitch,
    FilterList,
    Listen,
    QueryLogConfig,
    SafeSearch,
    StatsConfig,
    Switch,
)

__all__ = (
    'ACCESS',
    'BLOCKED_SERVICES',
    'BOOTSTRAP',
    'DEVICE_PLANE_COMMENT',
    'DNS_PORT',
    'FILTERING',
    'FILTERS',
    'FILTER_INTERVAL',
    'LISTEN',
    'PARENTAL',
    'QUERYLOG',
    'REVERSE_ZONES_COMMENT',
    'SAFEBROWSING',
    'SAFE_SEARCH',
    'STATS',
    'UPSTREAMS',
    'UPSTREAMS_COMMENT',
    'configuration',
    'dns_settings',
    'upstream_dns',
)

#: The port each instance answers DNS on, every address of the machine.
DNS_PORT = 53

#: Where the first-run setup binds an instance: its interface on every address
#: at the port the proxy, the flow rule and the `dns` stack's own endpoint all
#: meet on, and DNS on every address, both families included.
LISTEN: Listen = {
    'web': {'ip': '0.0.0.0', 'port': conventions.gateway.ADGUARD_API_PORT},
    'dns': {'ip': '0.0.0.0', 'port': DNS_PORT},
}

#: The public resolvers for every name outside the forwarded zones, over TLS,
#: each run by a different operator so that no single one's outage is the LAN's.
UPSTREAMS = ('tls://dns.quad9.net', 'tls://dns.google', 'tls://one.one.one.one')

#: The plain resolvers the upstreams' own names are looked up through.
BOOTSTRAP = ('9.9.9.10', '149.112.112.10', '2620:fe::10', '2620:fe::fe:10')

#: Hours between filter-list refreshes.
FILTER_INTERVAL = 24

#: A day, in the milliseconds the log settings are written in.
_DAY = 24 * 60 * 60 * 1000

#: The block lists, in the order the instances hold them. Every one is a list of
#: AdGuard's own registry, so an instance downloads it from there.
FILTERS: tuple[FilterList, ...] = (
    {
        'url': 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt',
        'name': 'AdGuard DNS filter',
        'enabled': True,
    },
    {
        'url': 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt',
        'name': 'AdAway Default Blocklist',
        'enabled': True,
    },
    {
        'url': 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_59.txt',
        'name': 'AdGuard DNS Popup Hosts filter',
        'enabled': False,
    },
    {
        'url': 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_34.txt',
        'name': "HaGeZi's Normal Blocklist",
        'enabled': True,
    },
    {
        'url': 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_60.txt',
        'name': "HaGeZi's Xiaomi Tracker Blocklist",
        'enabled': True,
    },
    {
        'url': 'https://adguardteam.github.io/HostlistsRegistry/assets/filter_21.txt',
        'name': 'CHN: anti-AD',
        'enabled': False,
    },
)

FILTERING: FilteringSwitch = {'enabled': True, 'interval': FILTER_INTERVAL}

#: Off, and so is parental control: the lists above are the blocking.
SAFEBROWSING: Switch = {'enabled': False}
PARENTAL: Switch = {'enabled': False}

#: Off, with every engine selected for when it is turned on.
SAFE_SEARCH: SafeSearch = {
    'enabled': False,
    'bing': True,
    'duckduckgo': True,
    'ecosia': True,
    'google': True,
    'pixabay': True,
    'yandex': True,
    'youtube': True,
}

#: No service blocked, on a schedule in the machine's own zone.
BLOCKED_SERVICES: BlockedServices = {'ids': [], 'schedule': {'time_zone': 'Local'}}

#: No client allowed or refused by address; the three names that report the
#: server's own version and identity are not answered.
ACCESS: AccessLists = {
    'allowed_clients': [],
    'disallowed_clients': [],
    'blocked_hosts': ['version.bind', 'id.server', 'hostname.bind'],
}

#: Ninety days of query log, with clients' addresses kept, and as long a window
#: of statistics.
QUERYLOG: QueryLogConfig = {
    'enabled': True,
    'interval': 90 * _DAY,
    'anonymize_client_ip': False,
    'ignored': [],
    'ignored_enabled': False,
}
STATS: StatsConfig = {'enabled': True, 'interval': 90 * _DAY, 'ignored': [], 'ignored_enabled': False}


#: The comment lines of the upstream list, as the instances hold them: one
#: above the device plane's line, one above the reverse zones' lines, one above
#: `UPSTREAMS`. The instance keeps a `#` line in the list and reports it back,
#: so they are declared like any other line of it.
DEVICE_PLANE_COMMENT = '# Forward .home.arpa queries to the DMSE'
REVERSE_ZONES_COMMENT = '# Forward Reverse DNS (IP-to-Name) for 10.x.x.x'
UPSTREAMS_COMMENT = '# Global DNS servers on the Internet'


def upstream_dns(*, forwarded_zones: Sequence[str], gateway: IPv4Address) -> list[str]:
    """The forwarded zones to the gateway's resolver, the device plane's first, then every other name to `UPSTREAMS`."""
    forwards = {zone: f'[/{zone}/]{gateway}' for zone in forwarded_zones}
    reverse = [line for zone, line in forwards.items() if zone.endswith('.in-addr.arpa')]
    device_plane = [line for zone, line in forwards.items() if not zone.endswith('.in-addr.arpa')]
    return [
        *((DEVICE_PLANE_COMMENT, *device_plane) if device_plane else ()),
        *((REVERSE_ZONES_COMMENT, *reverse) if reverse else ()),
        UPSTREAMS_COMMENT,
        *UPSTREAMS,
    ]


def dns_settings(*, forwarded_zones: Sequence[str], gateway: IPv4Address) -> DnsSettings:
    """The DNS server's settings: the forwarded zones to the gateway's resolver, every other name to `UPSTREAMS`.

    The gateway's resolver also answers the pointer lookups for the site's own
    addresses, so it is the private reverse resolver too.
    """
    return {
        'upstream_dns': upstream_dns(forwarded_zones=forwarded_zones, gateway=gateway),
        'upstream_dns_file': '',
        'bootstrap_dns': list(BOOTSTRAP),
        'fallback_dns': [],
        'upstream_mode': 'load_balance',
        'upstream_timeout': 10,
        'local_ptr_upstreams': [str(gateway)],
        'use_private_ptr_resolvers': True,
        'resolve_clients': True,
        'protection_enabled': True,
        'ratelimit': 20,
        'ratelimit_subnet_len_ipv4': 24,
        'ratelimit_subnet_len_ipv6': 56,
        'ratelimit_whitelist': [],
        'blocking_mode': 'default',
        'blocking_ipv4': '',
        'blocking_ipv6': '',
        'blocked_response_ttl': 10,
        'edns_cs_enabled': False,
        'edns_cs_use_custom': False,
        'edns_cs_custom_ip': '',
        'dnssec_enabled': True,
        'disable_ipv6': False,
        'cache_enabled': True,
        'cache_size': 4194304,
        'cache_ttl_min': 0,
        'cache_ttl_max': 0,
        'cache_optimistic': False,
    }


def configuration(
    *, blocks: Sequence[RuleBlock], forwarded_zones: Sequence[str], gateway: IPv4Address
) -> AdGuardConfiguration:
    """The one configuration both instances are handed: the rule list and its clients from `blocks`, the rest here."""
    return AdGuardConfiguration(
        listen=LISTEN,
        rules=user_rules(blocks),
        dns=dns_settings(forwarded_zones=forwarded_zones, gateway=gateway),
        access=ACCESS,
        filtering=FILTERING,
        safebrowsing=SAFEBROWSING,
        parental=PARENTAL,
        safe_search=SAFE_SEARCH,
        blocked_services=BLOCKED_SERVICES,
        filters=FILTERS,
        whitelist_filters=(),
        clients=blocklist_clients(blocks),
        querylog=QUERYLOG,
        stats=STATS,
    )
